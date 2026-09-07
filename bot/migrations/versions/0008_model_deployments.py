"""Model deployments — shadow/canary provenance (W7-22, §10.5).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-07

Numbered 0008 rather than the plan's 0007: ``deploy_events`` took that slot in
week 6 when the sixth citation kind turned out to need a table. Recorded here
rather than renumbered, because a migration that changes its own revision id
after being applied anywhere is a migration that cannot be rolled forward.

**What this table answers: "which configuration wrote this PIR?"**

``pir_documents`` already records the provider, the model, the prompt version
and the prompt hash for each document -- that is provenance for one row. This is
provenance for the *decision*: which configurations have been in flight, at what
traffic share, promoted by whom, and on the strength of which eval run. Without
it, "we moved to the cheaper model in October and quality held" is a memory
rather than a record, and the eval harness that proved it has no place to attach
its verdict to.

The promotion ladder it tracks:

    shadow -> canary 10% -> canary 50% -> default -> the previous one deprecated

**Canary routing is by hash of ``incident_id``, not by a random draw.** An
incident that flipped configuration halfway through would produce a PIR drafted
by one model and validated against a context assembled under another, and the
resulting row would be attributable to neither. Hashing the incident id makes
the assignment stable for the life of the incident, which is the only unit of
work here that lasts long enough for the distinction to matter.

``eval_run_id`` is nullable but the check constraint requires it once the state
reaches ``default``. Promoting to production without an eval run is exactly the
thing week 7 exists to prevent, and a promotion the database will not accept is
harder to do by accident than one only a runbook forbids.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_deployments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # The role, not the model, is the subject. `synthesize` is the thing
        # being deployed; the model is which implementation of it is in flight.
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=True),
        sa.Column("prompt_sha256", sa.Text, nullable=True),
        sa.Column(
            "state",
            sa.Text,
            nullable=False,
            server_default=sa.text("'shadow'"),
        ),
        # 0 for shadow (it runs alongside and is never posted), 100 for default.
        sa.Column(
            "traffic_pct",
            sa.SmallInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        # The evidence. A promotion with no eval run behind it is the failure
        # this whole week exists to make impossible.
        sa.Column("eval_run_id", sa.Text, nullable=True),
        sa.Column("eval_metrics", postgresql.JSONB, nullable=True),
        sa.Column("promoted_by", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Open-ended while in flight. NULL means "current", which is what the
        # partial unique index below keys on.
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('shadow', 'canary', 'default', 'deprecated', 'rolled_back')",
            name="ck_model_deployments_state",
        ),
        sa.CheckConstraint(
            "traffic_pct BETWEEN 0 AND 100",
            name="ck_model_deployments_traffic",
        ),
        # Shadow means "runs alongside, output stored, never posted". A shadow
        # deployment carrying real traffic is a canary that nobody labelled as
        # one, and the difference is user-visible risk.
        sa.CheckConstraint(
            "state <> 'shadow' OR traffic_pct = 0",
            name="ck_model_deployments_shadow_has_no_traffic",
        ),
        # The rule that matters. A configuration cannot become the default
        # without an eval run attached -- enforced by the schema rather than by
        # a runbook, because the schema is the one that is awake at 2 a.m.
        sa.CheckConstraint(
            "state <> 'default' OR eval_run_id IS NOT NULL",
            name="ck_model_deployments_default_needs_evidence",
        ),
    )

    # One live default per role. A partial unique index rather than application
    # logic: two rows both claiming to be the current `synthesize` default is
    # the state in which "which configuration wrote this PIR" stops having an
    # answer, and it is exactly the state a half-finished promotion produces.
    op.create_index(
        "uq_model_deployments_current_default",
        "model_deployments",
        ["role"],
        unique=True,
        postgresql_where=sa.text("state = 'default' AND ended_at IS NULL"),
    )

    op.create_index(
        "ix_model_deployments_role_started",
        "model_deployments",
        ["role", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_model_deployments_role_started", table_name="model_deployments")
    op.drop_index("uq_model_deployments_current_default", table_name="model_deployments")
    op.drop_table("model_deployments")
