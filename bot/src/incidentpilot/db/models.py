"""SQLAlchemy 2.0 mapped classes.

Only the tables week 2 touches are mapped. The rest exist in migrations and get
models when the code that uses them arrives -- an ORM class nobody queries is a
maintenance cost that looks like progress.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from incidentpilot.domain.normalize import SEVERITY_RANK
from incidentpilot.domain.states import S

# postgresql.ENUM, not sa.Enum: `create_type=False` is a PostgreSQL-dialect
# parameter. With the generic type it is silently ignored and SQLAlchemy emits
# CREATE TYPE again for every table that references the enum.
#
# The values are listed even though the type already exists in the database.
# Without them SQLAlchemy has no idea what is valid and rejects every bind with
# "'sev1' is not among the defined enum values ... Possible values: None" -- the
# type name alone is enough for DDL but not for INSERT.
#
# They come from the domain enums rather than string literals so the schema and
# the state machine cannot drift apart. The dependency arrow points the right
# way: db/ may import domain/, never the reverse (INV-01).
INCIDENT_STATE = postgresql.ENUM(*[s.value for s in S], name="incident_state", create_type=False)
SEVERITY_LEVEL = postgresql.ENUM(
    *sorted(SEVERITY_RANK, key=lambda level: -SEVERITY_RANK[level]),
    name="severity_level",
    create_type=False,
)
OUTBOX_STATUS = postgresql.ENUM(
    "pending",
    "claimed",
    "dispatched",
    "failed",
    "dead",
    name="outbox_status",
    create_type=False,
)

# Every timestamp is TIMESTAMPTZ, stored UTC (INV-08). Defined once so no column
# can accidentally be declared naive.
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    team: Mapped[str] = mapped_column(Text)
    tier: Mapped[int] = mapped_column(SmallInteger, server_default="2")
    slo_target: Mapped[float | None] = mapped_column(Numeric(6, 4))
    slo_window_days: Mapped[int] = mapped_column(SmallInteger, server_default="30")
    paging_service_ref: Mapped[str | None] = mapped_column(Text)
    depends_on: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default="{}")
    graph_depth: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Runbook(Base):
    __tablename__ = "runbooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    alert_pattern: Mapped[str] = mapped_column(Text)
    severity_filter: Mapped[str | None] = mapped_column(SEVERITY_LEVEL)
    service_filter: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    step_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    version: Mapped[str] = mapped_column(Text, server_default="1.0.0")
    git_sha: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Responder(Base):
    __tablename__ = "responders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    paging_user_ref: Mapped[str | None] = mapped_column(Text)
    team: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, server_default="UTC")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        # The dedup arbiter. Not a cache key -- a cache eviction must never be
        # able to create a second war room (B-07, INV-04).
        UniqueConstraint("dedup_key", "dedup_epoch", name="uq_incident_dedup"),
        CheckConstraint(
            "acknowledged_at IS NULL OR acknowledged_at >= detected_at",
            name="ck_ack_after_detect",
        ),
        CheckConstraint(
            "state <> 'merged' OR parent_incident_id IS NOT NULL",
            name="ck_merged_has_parent",
        ),
        Index("idx_inc_channel", "chat_channel_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_key: Mapped[str] = mapped_column(Text, unique=True)
    dedup_key: Mapped[str] = mapped_column(Text)
    dedup_epoch: Mapped[int] = mapped_column(Integer, server_default="0")
    parent_incident_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("incidents.id"))

    title: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(SEVERITY_LEVEL)
    severity_reason: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(INCIDENT_STATE, server_default="detected")
    state_seq: Mapped[int] = mapped_column(Integer, server_default="0")

    primary_service_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("services.id"))
    affected_services: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default="{}")
    root_signal: Mapped[str | None] = mapped_column(Text)
    correlated_alert_count: Mapped[int] = mapped_column(Integer, server_default="1")

    detected_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    engaged_at: Mapped[datetime | None] = mapped_column(TZ)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZ)
    mitigated_at: Mapped[datetime | None] = mapped_column(TZ)
    resolved_at: Mapped[datetime | None] = mapped_column(TZ)
    closed_at: Mapped[datetime | None] = mapped_column(TZ)
    last_alert_at: Mapped[datetime | None] = mapped_column(TZ)

    chat_channel_id: Mapped[str | None] = mapped_column(Text)
    chat_channel_name: Mapped[str | None] = mapped_column(Text)
    runbook_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("runbooks.id"))

    impact: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error_budget_burn: Mapped[float | None] = mapped_column(Numeric(8, 5))

    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    # GENERATED ALWAYS AS ... STORED, declared with Computed() so SQLAlchemy
    # knows never to emit them in an INSERT or UPDATE. There is exactly one
    # definition of MTTR in this system and it lives in the schema; the ORM must
    # not be able to write a different one.
    tta_seconds: Mapped[int | None] = mapped_column(
        Integer,
        Computed("EXTRACT(EPOCH FROM (acknowledged_at - detected_at))::INT", persisted=True),
    )
    ttm_seconds: Mapped[int | None] = mapped_column(
        Integer,
        Computed("EXTRACT(EPOCH FROM (mitigated_at - detected_at))::INT", persisted=True),
    )
    mttr_seconds: Mapped[int | None] = mapped_column(
        Integer,
        Computed("EXTRACT(EPOCH FROM (resolved_at - detected_at))::INT", persisted=True),
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("fingerprint", "starts_at", name="uq_alert_firing"),
        Index("idx_alerts_incident", "incident_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)
    alertname: Mapped[str] = mapped_column(Text)
    service: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    starts_at: Mapped[datetime] = mapped_column(TZ)
    ends_at: Mapped[datetime | None] = mapped_column(TZ)
    is_root_signal: Mapped[bool] = mapped_column(Boolean, server_default="false")
    merge_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    merge_reasons: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    # X-02: Alertmanager re-sends with the same startsAt, so without these the
    # retry is a silent no-op and received_at holds the first receipt forever.
    last_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    seen_count: Mapped[int] = mapped_column(Integer, server_default="1")
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)


class IncidentTransition(Base):
    __tablename__ = "incident_transitions"
    __table_args__ = (
        # Two workers cannot both write seq = N. The Python transition table
        # catches programmer error; this catches concurrency error.
        UniqueConstraint("incident_id", "seq", name="uq_transition_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    from_state: Mapped[str] = mapped_column(INCIDENT_STATE)
    to_state: Mapped[str] = mapped_column(INCIDENT_STATE)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(OUTBOX_STATUS, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    dispatched_at: Mapped[datetime | None] = mapped_column(TZ)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class CorrelationFeedback(Base):
    __tablename__ = "correlation_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE")
    )
    alert_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("alerts.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text)
    original_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    original_reasons: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    actor: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class TimelineEvent(Base):
    """Hypertable. No foreign key by design -- FK enforcement across many chunks
    is a chunk-management cost with little payoff; integrity is the
    application's job plus a periodic orphan check."""

    __tablename__ = "timeline_events"

    time: Mapped[datetime] = mapped_column(TZ, primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent: Mapped[str] = mapped_column(Text, primary_key=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    description: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[str | None] = mapped_column(Text)
    source_message_ts: Mapped[str | None] = mapped_column(Text)


class SignalSample(Base):
    __tablename__ = "signal_samples"

    time: Mapped[datetime] = mapped_column(TZ, primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series: Mapped[str] = mapped_column(Text, primary_key=True)
    service: Mapped[str] = mapped_column(Text)
    value: Mapped[float] = mapped_column(Float)


class SlackMessage(Base):
    """The transcript. Our system of record for what was said (B-01, INV-02).

    Written as each message arrives, never read back from Slack. Since 3 March
    2026 ``conversations.history`` gives a non-Marketplace app 1 request/minute
    and 15 messages per request, so reconstructing a 150-message incident at
    resolve time would take ten minutes -- and would fail *silently*, returning
    less than it used to with no error at all.

    ``UNIQUE (channel_id, ts)`` is what turns Slack's at-least-once event
    delivery into exactly-once storage. It is a constraint rather than a cache
    check because a redelivery six hours later must still be rejected (INV-04).
    """

    __tablename__ = "slack_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "ts", name="uq_message_channel_ts"),
        Index("idx_msg_incident", "incident_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE")
    )
    channel_id: Mapped[str] = mapped_column(Text)
    # Slack's own id, stored as text: it is the citation anchor (``msg:{ts}``)
    # and float round-tripping would eventually collide two messages a
    # microsecond apart.
    ts: Mapped[str] = mapped_column(Text)
    thread_ts: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    redacted_text: Mapped[str | None] = mapped_column(Text)  # what a model may see (W6, D6)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class SlackMessageRevision(Base):
    """Edits and deletes, appended -- never applied in place.

    A PIR claim cites ``msg:{ts}``. If that message could be silently rewritten
    the citation would stop being evidence: the document would say one thing,
    the linked message another, and nothing would record that they ever agreed.
    So the original row is immutable and every mutation becomes a row here.

    ``UNIQUE (message_id, revision)`` makes a redelivered ``message_changed``
    event a no-op instead of a second revision of the same edit.
    """

    __tablename__ = "slack_message_revisions"
    __table_args__ = (UniqueConstraint("message_id", "revision", name="uq_message_revision"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("slack_messages.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)  # 'edited' | 'deleted'
    text: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
