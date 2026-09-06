"""Incident core: incidents, alerts, incident_transitions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-06

Five of the constraints here ARE the correctness mechanism of a whole subsystem,
not hygiene:

  UNIQUE (dedup_key, dedup_epoch)   prevents duplicate war rooms (B-07, INV-04)
  UNIQUE (fingerprint, starts_at)   makes alert ingestion idempotent
  UNIQUE (incident_id, seq)         prevents two workers both advancing state

MTTA/TTM/MTTR are GENERATED columns so there is exactly one definition of each
and it lives in the schema. Every "our MTTR numbers disagree" argument comes
from two services computing it differently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("public_key", sa.Text, nullable=False, unique=True),
        sa.Column("dedup_key", sa.Text, nullable=False),
        # Bumps when an incident closes, so the same alert recurring next week
        # creates a new incident instead of colliding with a closed one. See
        # X-03: no source contained the code that writes this, so the repository
        # derives it inside the transaction that holds the correlation lock.
        sa.Column("dedup_epoch", sa.Integer, nullable=False, server_default="0"),
        sa.Column("parent_incident_id", sa.BigInteger, sa.ForeignKey("incidents.id")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column(
            "severity", postgresql.ENUM(name="severity_level", create_type=False), nullable=False
        ),
        # Auditable, not folklore. Severity drives paging, so "why was this a
        # Sev1" must be answerable from the row.
        sa.Column("severity_reason", sa.Text),
        sa.Column(
            "state",
            postgresql.ENUM(name="incident_state", create_type=False),
            nullable=False,
            server_default="detected",
        ),
        sa.Column("state_seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("primary_service_id", sa.Integer, sa.ForeignKey("services.id")),
        sa.Column(
            "affected_services",
            sa.ARRAY(sa.Integer),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("root_signal", sa.Text),
        sa.Column("correlated_alert_count", sa.Integer, nullable=False, server_default="1"),
        # Every timestamp is TIMESTAMPTZ (INV-08, B-06). This product's entire
        # output is a timeline and responders sit in IST, PT and CET.
        sa.Column(
            "detected_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("engaged_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("mitigated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_alert_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("chat_channel_id", sa.Text),
        sa.Column("chat_channel_name", sa.Text),
        sa.Column("runbook_id", sa.Integer, sa.ForeignKey("runbooks.id")),
        # Computed deterministically from PromQL, NEVER model-generated (B-10).
        sa.Column("impact", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_budget_burn", sa.Numeric(8, 5)),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("dedup_key", "dedup_epoch", name="uq_incident_dedup"),
        sa.CheckConstraint(
            "acknowledged_at IS NULL OR acknowledged_at >= detected_at",
            name="ck_ack_after_detect",
        ),
        sa.CheckConstraint(
            "mitigated_at IS NULL OR mitigated_at >= detected_at", name="ck_mitigate_after_detect"
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= detected_at", name="ck_resolve_after_detect"
        ),
        sa.CheckConstraint(
            "state <> 'merged' OR parent_incident_id IS NOT NULL", name="ck_merged_has_parent"
        ),
    )

    # pgvector has no SQLAlchemy core type, so the column is added with raw
    # DDL rather than through a placeholder that would have to be swapped.
    op.execute("ALTER TABLE incidents ADD COLUMN embedding vector(1536)")

    # Generated columns: MTTR can never drift from the timestamps it is derived
    # from, because it is not derived by anybody -- it is the schema.
    op.execute(
        """
        ALTER TABLE incidents
          ADD COLUMN tta_seconds  INT GENERATED ALWAYS AS
            (EXTRACT(EPOCH FROM (acknowledged_at - detected_at))::INT) STORED,
          ADD COLUMN ttm_seconds  INT GENERATED ALWAYS AS
            (EXTRACT(EPOCH FROM (mitigated_at   - detected_at))::INT) STORED,
          ADD COLUMN mttr_seconds INT GENERATED ALWAYS AS
            (EXTRACT(EPOCH FROM (resolved_at    - detected_at))::INT) STORED
        """
    )

    # Partial indexes carry the dashboard load: the working set is a handful of
    # open incidents against months of closed ones.
    op.execute(
        """
        CREATE INDEX idx_inc_open ON incidents (severity, detected_at DESC)
          WHERE state NOT IN ('closed','merged','false_positive','abandoned')
        """
    )
    op.execute("CREATE INDEX idx_inc_channel ON incidents (chat_channel_id)")
    op.execute(
        "CREATE INDEX idx_inc_parent ON incidents (parent_incident_id) "
        "WHERE parent_incident_id IS NOT NULL"
    )
    op.execute("CREATE INDEX idx_inc_dedup_open ON incidents (dedup_key, dedup_epoch)")
    op.execute("CREATE INDEX idx_inc_embed ON incidents USING hnsw (embedding vector_cosine_ops)")

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("alertname", sa.Text, nullable=False),
        sa.Column("service", sa.Text),
        sa.Column("labels", postgresql.JSONB, nullable=False),
        sa.Column(
            "annotations",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ends_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("is_root_signal", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("merge_score", sa.Numeric(4, 3)),
        # Explainability, surfaced in Slack. Opaque grouping is what people
        # distrust about commercial tools.
        sa.Column(
            "merge_reasons",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # X-02: Alertmanager re-sends a firing alert every repeat_interval with
        # the SAME startsAt -- that field is when the alert began, not when it
        # was sent. The unique constraint below is the correct idempotency
        # guard, but without these two columns the retry is a silent no-op and
        # received_at holds the first receipt forever. "Alertmanager re-sent
        # this four times before we mitigated" is a real PIR sentence.
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("seen_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("raw", postgresql.JSONB, nullable=False),
        sa.UniqueConstraint("fingerprint", "starts_at", name="uq_alert_firing"),
    )
    op.execute("CREATE INDEX idx_alerts_incident ON alerts (incident_id, starts_at)")
    op.execute("CREATE INDEX idx_alerts_root ON alerts (incident_id) WHERE is_root_signal")

    op.create_table(
        "incident_transitions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column(
            "from_state", postgresql.ENUM(name="incident_state", create_type=False), nullable=False
        ),
        sa.Column(
            "to_state", postgresql.ENUM(name="incident_state", create_type=False), nullable=False
        ),
        sa.Column("actor", sa.Text, nullable=False),  # 'system' or a slack_user_id
        sa.Column("reason", sa.Text),
        sa.Column(
            "occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # The concurrency guard. The Python transition table catches programmer
        # error at development time; this catches concurrency error at 3 a.m.
        sa.UniqueConstraint("incident_id", "seq", name="uq_transition_seq"),
    )


def downgrade() -> None:
    op.drop_table("incident_transitions")
    op.drop_table("alerts")
    op.execute("DROP INDEX IF EXISTS idx_inc_embed")
    op.execute("DROP INDEX IF EXISTS idx_inc_dedup_open")
    op.execute("DROP INDEX IF EXISTS idx_inc_parent")
    op.execute("DROP INDEX IF EXISTS idx_inc_channel")
    op.execute("DROP INDEX IF EXISTS idx_inc_open")
    op.drop_table("incidents")
