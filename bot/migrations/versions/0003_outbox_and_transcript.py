"""Outbox, correlation feedback, and the transcript tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06

Plan ordering note: the file manifest assigns this migration to week 3, but
``orchestrator.transition()`` is a week 2 task and enqueues to ``outbox_events``
in the same transaction as the state change -- it cannot write to a table that
does not exist yet. Creating the whole migration now rather than splitting it
keeps the plan's numbering intact; ``slack_messages`` simply stays empty until
week 4 fills it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        # sha256(incident_id|action|discriminator). This is what licenses the
        # retry: without it, retrying conversations.create makes a second
        # channel, and the orphaned-channel bug is back.
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="outbox_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("result", postgresql.JSONB),
    )
    # Partial: the relay only ever claims pending rows, and the table is mostly
    # dispatched history.
    op.execute(
        "CREATE INDEX idx_outbox_claim ON outbox_events (next_attempt_at) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX idx_outbox_dead ON outbox_events (created_at DESC) WHERE status = 'dead'"
    )

    # The labelled data that tunes merge_threshold and feeds the eval corpus.
    # A correlation engine without a correction path drifts and nobody notices.
    op.create_table(
        "correlation_feedback",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alert_id", sa.BigInteger, sa.ForeignKey("alerts.id", ondelete="SET NULL")),
        sa.Column("action", sa.Text, nullable=False),  # 'split' | 'merge'
        sa.Column("original_score", sa.Numeric(4, 3)),
        sa.Column(
            "original_reasons",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "slack_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.Text, nullable=False),
        # Slack's own id, and the citation anchor every PIR claim points at.
        sa.Column("ts", sa.Text, nullable=False),
        sa.Column("thread_ts", sa.Text),
        sa.Column("user_id", sa.Text),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("redacted_text", sa.Text),  # what actually goes to a model
        sa.Column("raw", postgresql.JSONB, nullable=False),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # At-least-once delivery becomes exactly-once storage. The Events API
        # redelivers; ON CONFLICT DO NOTHING plus a counter is the whole fix.
        sa.UniqueConstraint("channel_id", "ts", name="uq_message_channel_ts"),
    )
    op.execute("CREATE INDEX idx_msg_incident ON slack_messages (incident_id, ts)")
    op.execute(
        "CREATE INDEX idx_msg_fts ON slack_messages USING gin (to_tsvector('english', text))"
    )

    # Revisions are appended, never applied in place. A PIR cites msg:{ts}; if
    # that message could be silently rewritten, the citation would stop being
    # evidence.
    op.create_table(
        "slack_message_revisions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "message_id",
            sa.BigInteger,
            sa.ForeignKey("slack_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),  # 'edited' | 'deleted'
        sa.Column("text", sa.Text),
        sa.Column(
            "observed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("message_id", "revision", name="uq_message_revision"),
    )


def downgrade() -> None:
    op.drop_table("slack_message_revisions")
    op.execute("DROP INDEX IF EXISTS idx_msg_fts")
    op.execute("DROP INDEX IF EXISTS idx_msg_incident")
    op.drop_table("slack_messages")
    op.drop_table("correlation_feedback")
    op.execute("DROP INDEX IF EXISTS idx_outbox_dead")
    op.execute("DROP INDEX IF EXISTS idx_outbox_claim")
    op.drop_table("outbox_events")
