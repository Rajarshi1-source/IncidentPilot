"""Deploy events — the sixth citation kind (W6-22, D1).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07

The evidence grammar names six citable kinds and five of them already have a
home: messages, timeline events, alerts, runbook step signals, and computed
metric windows. ``deploy:{sha}`` had none -- §10.2 says only "deploy webhook
events", and a citation kind with nowhere to resolve from is a citation kind the
validator must reject on every draft.

Which matters more than the row count suggests. "Deployed twelve minutes before
detection" is the single most useful sentence a postmortem can contain, and it
is the one claim a model is most likely to make and least able to support.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deploy_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        # Normalized to twelve lowercase characters on write. A webhook sends
        # forty and a human writes seven; two ids for one deploy would make half
        # the citations to it look fabricated.
        sa.Column("sha", sa.Text, nullable=False),
        sa.Column("service", sa.Text, nullable=False),
        sa.Column("environment", sa.Text, nullable=False, server_default="production"),
        sa.Column("repository", sa.Text),
        sa.Column("actor", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("deployed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # CI retries a failed notification step, and the same commit is deployed
        # to staging and to production. (sha, service, environment) is the
        # identity; without it a retry would create a second row and the same
        # deploy would be citable twice with different ids.
        sa.UniqueConstraint("sha", "service", "environment", name="uq_deploy_identity"),
    )
    # The only query that matters: "what shipped to this service just before the
    # incident started". Descending, because the answer is always the newest few.
    op.execute("CREATE INDEX idx_deploy_lookup ON deploy_events (service, deployed_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_deploy_lookup")
    op.drop_table("deploy_events")
