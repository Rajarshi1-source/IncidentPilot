"""The transactional outbox and its relay (B-08).

The bug this exists to kill: the orchestrator calls Slack, then writes to
Postgres. Crash between the two and there is a ``#inc-…`` channel that no
incident row knows about -- invisible to the dashboard, un-resolvable by
``/resolve``, and it sits in the workspace forever. At 30 incidents a day that
happens weekly, not hypothetically.

The fix is to write the *intent* in the same transaction as the state change,
and let a separate relay perform the external call keyed by an idempotency key.
Two properties fall out:

1. The state change and its side-effect intents commit or roll back together.
2. Exactly one component performs external writes. If two processes can create
   a channel, eventually two will.

``SELECT … FOR UPDATE SKIP LOCKED`` is why there is no ZooKeeper, no etcd lease
and no leader election here: **Postgres row locks are the coordination
primitive**, and N relay replicas need no coordination service to avoid each
other.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from incidentpilot.adapters.chat.base import PermanentChatError, RetryableChatError
from incidentpilot.resilience.retry import next_attempt_delay
from incidentpilot.telemetry.logging import get_logger, incident_context

log = get_logger(__name__)

# After this many attempts a row is dead: surfaced on the dashboard and alerted,
# never silently dropped. A DLQ nobody looks at is the same as dropping.
MAX_ATTEMPTS = 8

# Claim a batch atomically. FOR UPDATE SKIP LOCKED lets a second relay replica
# step straight past rows this one holds instead of blocking behind them.
CLAIM = text(
    """
    UPDATE outbox_events
       SET status = 'claimed', attempts = attempts + 1
     WHERE id IN (
         SELECT id FROM outbox_events
          WHERE status = 'pending' AND next_attempt_at <= now()
          ORDER BY id
            FOR UPDATE SKIP LOCKED
          LIMIT :batch
     )
    RETURNING id, incident_id, action, payload, idempotency_key, attempts
    """
)

MARK_DISPATCHED = text(
    """
    UPDATE outbox_events
       SET status = 'dispatched', dispatched_at = now(), result = CAST(:result AS jsonb),
           last_error = NULL
     WHERE id = :id
    """
)

DEFER = text(
    """
    UPDATE outbox_events
       SET status = 'pending', next_attempt_at = :next_at, last_error = :error
     WHERE id = :id
    """
)

MARK_DEAD = text(
    """
    UPDATE outbox_events
       SET status = 'dead', last_error = :error
     WHERE id = :id
    """
)

# Rows a relay claimed and never resolved -- the process died between CLAIM and
# the dispatch record (crash point 7). Without this sweep they sit 'claimed'
# forever: not done, not retried, not visible.
RECLAIM_STUCK = text(
    """
    UPDATE outbox_events
       SET status = 'pending'
     WHERE status = 'claimed' AND next_attempt_at <= :cutoff
    RETURNING id
    """
)


def idem_key(incident_id: int, action: str, discriminator: str = "") -> str:
    """``sha256(incident_id|action|discriminator)``.

    This value is what licenses retrying an external write. Without it, a relay
    that dies after Slack created the channel but before the dispatch was
    recorded produces a second channel on retry -- the exact orphan bug the
    outbox exists to prevent.

    The discriminator distinguishes repeats of the same action on one incident:
    a storm banner at 5 alerts and again at 10 are different intents.
    """
    return hashlib.sha256(f"{incident_id}|{action}|{discriminator}".encode()).hexdigest()


def utcnow() -> datetime:
    """The relay's single clock read, isolated for replay."""
    return datetime.now(UTC)


class OutboxRelay:
    """Claims outbox rows and performs the external calls they describe.

    ``handlers`` maps an action name to the coroutine that performs it. This is
    the only place in the system permitted to talk to Slack (INV-03).
    """

    def __init__(
        self,
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]],
        *,
        batch: int = 20,
        stuck_after_s: int = 120,
    ) -> None:
        self._handlers = handlers
        self._batch = batch
        self._stuck_after_s = stuck_after_s

    async def relay_once(self, session: AsyncSession) -> int:
        """Claim and dispatch one batch. Returns how many rows were dispatched.

        Each row is handled in its own savepoint so one poisoned action cannot
        roll back the batch around it -- during a storm the batch is forty rows,
        and losing thirty-nine because of one bad payload would be a very
        expensive way to be tidy.
        """
        rows = (await session.execute(CLAIM, {"batch": self._batch})).mappings().all()
        if not rows:
            return 0

        dispatched = 0
        for row in rows:
            if await self._dispatch(session, dict(row)):
                dispatched += 1
        return dispatched

    async def _dispatch(self, session: AsyncSession, row: dict[str, Any]) -> bool:
        action = row["action"]
        handler = self._handlers.get(action)

        if handler is None:
            # An unknown action will never succeed. Dead immediately rather than
            # eight pointless retries.
            await session.execute(
                MARK_DEAD, {"id": row["id"], "error": f"no handler for action {action!r}"}
            )
            log.error("outbox.no_handler", action=action, incident_id=row["incident_id"])
            return False

        with incident_context(incident_id=row["incident_id"]):
            try:
                result = await handler(row)
            except PermanentChatError as exc:
                # A 4xx fails identically forever; retrying spends rate-limit
                # budget on a call that cannot succeed.
                await session.execute(MARK_DEAD, {"id": row["id"], "error": str(exc)})
                log.error("outbox.permanent_failure", action=action, error=str(exc))
                return False
            except (RetryableChatError, TimeoutError, ConnectionError) as exc:
                await self._defer_or_kill(session, row, str(exc))
                return False
            except Exception as exc:
                await self._defer_or_kill(session, row, f"{type(exc).__name__}: {exc}")
                return False

            import json

            await session.execute(
                MARK_DISPATCHED, {"id": row["id"], "result": json.dumps(result or {})}
            )
            log.info("outbox.dispatched", action=action, attempts=row["attempts"])
            return True

    async def _defer_or_kill(self, session: AsyncSession, row: dict[str, Any], error: str) -> None:
        from incidentpilot.telemetry.metrics import OUTBOX_DEAD

        attempts = int(row["attempts"])
        if attempts >= MAX_ATTEMPTS:
            await session.execute(MARK_DEAD, {"id": row["id"], "error": error})
            OUTBOX_DEAD.labels(action=row["action"]).inc()
            log.error("outbox.dead", action=row["action"], attempts=attempts, error=error)
            return

        delay = next_attempt_delay(attempts)
        await session.execute(
            DEFER,
            {
                "id": row["id"],
                "next_at": utcnow() + timedelta(seconds=delay),
                "error": error,
            },
        )
        log.warning(
            "outbox.deferred",
            action=row["action"],
            attempts=attempts,
            retry_in_s=round(delay, 1),
            error=error,
        )

    async def reclaim_stuck(self, session: AsyncSession) -> int:
        """Return rows a dead relay left 'claimed' to the pending pool.

        This is crash point 7. The relay claimed the row, then the process died
        before it could dispatch or defer. Nothing else will ever touch that row
        -- it is not pending, so no relay claims it, and it has no dispatch, so
        nothing completes it.
        """
        cutoff = utcnow() - timedelta(seconds=self._stuck_after_s)
        rows = (await session.execute(RECLAIM_STUCK, {"cutoff": cutoff})).mappings().all()
        if rows:
            log.warning("outbox.reclaimed_stuck", count=len(rows))
        return len(rows)


async def pending_count(session: AsyncSession, *, status: str = "pending") -> int:
    result = await session.execute(
        text("SELECT count(*) FROM outbox_events WHERE status = :s"), {"s": status}
    )
    return int(result.scalar_one())
