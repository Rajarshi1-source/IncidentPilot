"""PIR documents, action items, and runbook step signals.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-07

Plan ordering note, same shape as 0003. The manifest assigns this migration to
week 6 (W6-06), but ``runbooks/detector.py`` is a week 5 task and writes
``runbook_step_signals`` -- and ``Q5``, the corrected dead-step query, reads it.
Creating the whole migration now rather than splitting it keeps the plan's
numbering intact; ``pir_documents`` and ``action_items`` simply stay empty until
week 6 fills them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pir_documents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        # llm_primary | llm_secondary | skeleton. Recorded rather than inferred:
        # "which layer produced this" is the honest quality signal, and a
        # document that silently fell back to the skeleton must say so.
        sa.Column("generation_layer", sa.Text, nullable=False),
        sa.Column("body", postgresql.JSONB, nullable=False),
        sa.Column("rendered_markdown", sa.Text),
        sa.Column("provider", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("prompt_sha256", sa.Text),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("generation_ms", sa.Integer),
        # Must be 1.000 to publish (INV-05, D1). Stored so the claim is
        # auditable after the fact rather than only asserted at generation time.
        sa.Column("citation_coverage", sa.Numeric(4, 3)),
        sa.Column("validation_passed", sa.Boolean, nullable=False),
        sa.Column(
            "validation_errors", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column("human_edit_ratio", sa.Numeric(4, 3)),
        sa.Column("reviewed_by", sa.Text),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("incident_id", "revision", name="uq_pir_revision"),
    )

    op.create_table(
        "action_items",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pir_id", sa.BigInteger, sa.ForeignKey("pir_documents.id", ondelete="SET NULL")),
        sa.Column("description", sa.Text, nullable=False),
        # Which message proposed it. An action item nobody can trace back to a
        # sentence someone actually said is the same fabrication problem the PIR
        # citations exist to prevent.
        sa.Column("citations", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("owner", sa.Text),
        sa.Column("priority", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("external_ref", sa.Text),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("reopened_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        "CREATE INDEX idx_ai_open ON action_items (priority, created_at) WHERE status = 'open'"
    )

    # D4's substrate. One row per *executed step* (C-07), which is why adherence
    # is computed by counting rows rather than by reading a steps_followed
    # column that never existed.
    op.create_table(
        "runbook_step_signals",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "incident_id",
            sa.BigInteger,
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runbook_id", sa.Integer, sa.ForeignKey("runbooks.id"), nullable=False),
        sa.Column("step_id", sa.Text, nullable=False),
        # 'command_match' | 'reaction' | 'slash_command'. Three sources, and the
        # unique constraint below is what collapses them to one row: a responder
        # who runs the command, reacts to the pinned runbook AND types
        # `/step done` has followed the step once, not three times.
        sa.Column("detected_by", sa.Text, nullable=False),
        sa.Column(
            "observed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("source_message_ts", sa.Text),
        sa.UniqueConstraint("incident_id", "step_id", name="uq_step_signal"),
    )


def downgrade() -> None:
    op.drop_table("runbook_step_signals")
    op.execute("DROP INDEX IF EXISTS idx_ai_open")
    op.drop_table("action_items")
    op.drop_table("pir_documents")
