"""The repositories of one Postgres unit of work, all on its transaction (`Session`).

Locking, so that units of work on separate connections behave like the memory store's
serialized ones where it matters, without waiting on each other where it does not:

- The load row is the lock for the load and its risk tracker: `loads.get` and
  `get_tracker` lock it (`FOR NO KEY UPDATE`) until the unit of work ends. A
  read-then-write of either cannot lose a concurrent update; the other unit of work
  waits. `NO KEY` lets foreign key checks of new rows of the load go through meanwhile.
- The obligation row is the lock for the obligation and its timers: `obligations.get`
  and `get_open` lock it. `timers.due` locks each due timer together with its
  obligation and skips (`SKIP LOCKED`) those another unit of work holds, so the timer
  scheduler never waits on a run that is changing them; the next tick picks them up.
  This keeps one lock order (obligation, then timers) and so no deadlock between them.
- `queue.claim_next` skips locked rows, and a partial unique index allows one event
  in flight per load (see `PostgresEventQueue`). `queue.reclaim_expired` skips locked
  rows too.
- Every other read (lists, `recent`, `for_event`, the outbox) takes no lock.

Keep units of work short: a lock is held until the unit of work ends.
"""

from datetime import datetime, timedelta
from typing import Final

from psycopg import errors

from freight_agent.adapters.persistence.postgres import rows
from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.events import Event
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation, ObligationStatus
from freight_agent.domain.outbox import OutboxItem, OutboxStatus
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import Timer, TimerStatus

_EVENT: Final = (
    "event_id, load_id, type, occurred_at, channel, direction, sender_contact_id, body,"
    " attachments, data"
)
_EVENT_E: Final = (
    "e.event_id, e.load_id, e.type, e.occurred_at, e.channel, e.direction,"
    " e.sender_contact_id, e.body, e.attachments, e.data"
)
_EVENT_VALUES: Final = (
    "%(event_id)s, %(load_id)s, %(type)s, %(occurred_at)s, %(channel)s, %(direction)s,"
    " %(sender_contact_id)s, %(body)s, %(attachments)s, %(data)s"
)
_OBLIGATION: Final = (
    "obligation_id, load_id, kind, status, attempts, opened_at, last_action_at,"
    " deferred_until, deferrals_used, active_timer_id, sop_version"
)
_TIMER: Final = "timer_id, load_id, obligation_id, due_at, created_at, status, reason"
_OUTBOX: Final = (
    "outbox_id, idempotency_key, run_id, load_id, kind, payload, created_at, status,"
    " attempts, last_error, accepted_at"
)

_PENDING: Final = TimerStatus.PENDING.value


class PostgresLoadRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def add(self, load: Load) -> bool:
        row = await self._s.one(
            "INSERT INTO loads (load_id, broker_id, stage, delay_risk, metadata)"
            " VALUES (%(load_id)s, %(broker_id)s, %(stage)s, %(delay_risk)s, %(metadata)s)"
            " ON CONFLICT (load_id) DO NOTHING RETURNING load_id",
            rows.load_params(load),
        )
        return row is not None

    async def get(self, load_id: str) -> Load | None:
        row = await self._s.one(
            "SELECT metadata FROM loads WHERE load_id = %s FOR NO KEY UPDATE", (load_id,)
        )
        return None if row is None else rows.load_from(row)

    async def save(self, load: Load) -> None:
        await self._s.run(
            "INSERT INTO loads (load_id, broker_id, stage, delay_risk, metadata)"
            " VALUES (%(load_id)s, %(broker_id)s, %(stage)s, %(delay_risk)s, %(metadata)s)"
            " ON CONFLICT (load_id) DO UPDATE SET broker_id = EXCLUDED.broker_id,"
            " stage = EXCLUDED.stage, delay_risk = EXCLUDED.delay_risk,"
            " metadata = EXCLUDED.metadata",
            rows.load_params(load),
        )

    async def list_ids(self) -> list[str]:
        found = await self._s.all("SELECT load_id FROM loads ORDER BY load_id")
        return [row["load_id"] for row in found]

    async def get_tracker(self, load_id: str) -> RiskTracker | None:
        # Locks the load's row, not the tracker's: a load's first tracker has no row yet.
        row = await self._s.one(
            "SELECT t.band, t.pending, t.pending_count, t.recent_ping_ids"
            " FROM loads l LEFT JOIN risk_trackers t ON t.load_id = l.load_id"
            " WHERE l.load_id = %s FOR NO KEY UPDATE OF l",
            (load_id,),
        )
        return None if row is None or row["band"] is None else rows.tracker_from(row)

    async def save_tracker(self, load_id: str, tracker: RiskTracker) -> None:
        await self._s.run(
            "INSERT INTO risk_trackers (load_id, band, pending, pending_count, recent_ping_ids)"
            " VALUES (%(load_id)s, %(band)s, %(pending)s, %(pending_count)s,"
            " %(recent_ping_ids)s)"
            " ON CONFLICT (load_id) DO UPDATE SET band = EXCLUDED.band,"
            " pending = EXCLUDED.pending, pending_count = EXCLUDED.pending_count,"
            " recent_ping_ids = EXCLUDED.recent_ping_ids",
            rows.tracker_params(load_id, tracker),
        )


class PostgresEventLog:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def append(self, event: Event) -> bool:
        # DO NOTHING, not a caught error: a failed INSERT would abort the transaction.
        row = await self._s.one(
            f"INSERT INTO events ({_EVENT}) VALUES ({_EVENT_VALUES})"
            " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            rows.event_params(event),
        )
        return row is not None

    async def get(self, event_id: str) -> Event | None:
        row = await self._s.one(f"SELECT {_EVENT} FROM events WHERE event_id = %s", (event_id,))
        return None if row is None else rows.event_from(row)

    async def recent(self, load_id: str, limit: int) -> list[Event]:
        if limit <= 0:
            return []
        found = await self._s.all(
            f"SELECT {_EVENT} FROM ("
            f"  SELECT seq, {_EVENT} FROM events WHERE load_id = %s ORDER BY seq DESC LIMIT %s"
            ") AS newest ORDER BY seq",
            (load_id, limit),
        )
        return [rows.event_from(row) for row in found]


class PostgresObligationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def get(self, obligation_id: str) -> Obligation | None:
        row = await self._s.one(
            f"SELECT {_OBLIGATION} FROM obligations WHERE obligation_id = %s FOR NO KEY UPDATE",
            (obligation_id,),
        )
        return None if row is None else rows.obligation_from(row)

    async def get_open(self, load_id: str, kind: str) -> Obligation | None:
        row = await self._s.one(
            f"SELECT {_OBLIGATION} FROM obligations"
            " WHERE load_id = %s AND kind = %s AND status = %s"
            " ORDER BY opened_at, id_order(obligation_id), obligation_id"
            " LIMIT 1 FOR NO KEY UPDATE",
            (load_id, kind, ObligationStatus.OPEN.value),
        )
        return None if row is None else rows.obligation_from(row)

    async def list_for_load(self, load_id: str) -> list[Obligation]:
        found = await self._s.all(
            f"SELECT {_OBLIGATION} FROM obligations WHERE load_id = %s"
            " ORDER BY opened_at, id_order(obligation_id), obligation_id",
            (load_id,),
        )
        return [rows.obligation_from(row) for row in found]

    async def save(self, obligation: Obligation) -> None:
        await self._s.run(
            f"INSERT INTO obligations ({_OBLIGATION}) VALUES ("
            "  %(obligation_id)s, %(load_id)s, %(kind)s, %(status)s, %(attempts)s,"
            "  %(opened_at)s, %(last_action_at)s, %(deferred_until)s, %(deferrals_used)s,"
            "  %(active_timer_id)s, %(sop_version)s"
            ") ON CONFLICT (obligation_id) DO UPDATE SET load_id = EXCLUDED.load_id,"
            " kind = EXCLUDED.kind, status = EXCLUDED.status, attempts = EXCLUDED.attempts,"
            " opened_at = EXCLUDED.opened_at, last_action_at = EXCLUDED.last_action_at,"
            " deferred_until = EXCLUDED.deferred_until,"
            " deferrals_used = EXCLUDED.deferrals_used,"
            " active_timer_id = EXCLUDED.active_timer_id, sop_version = EXCLUDED.sop_version",
            rows.obligation_params(obligation),
        )


class PostgresTimerRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def get(self, timer_id: str) -> Timer | None:
        row = await self._s.one(f"SELECT {_TIMER} FROM timers WHERE timer_id = %s", (timer_id,))
        return None if row is None else rows.timer_from(row)

    async def save(self, timer: Timer) -> None:
        await self._s.run(
            f"INSERT INTO timers ({_TIMER}) VALUES ("
            "  %(timer_id)s, %(load_id)s, %(obligation_id)s, %(due_at)s, %(created_at)s,"
            "  %(status)s, %(reason)s"
            ") ON CONFLICT (timer_id) DO UPDATE SET load_id = EXCLUDED.load_id,"
            " obligation_id = EXCLUDED.obligation_id, due_at = EXCLUDED.due_at,"
            " created_at = EXCLUDED.created_at, status = EXCLUDED.status,"
            " reason = EXCLUDED.reason",
            rows.timer_params(timer),
        )

    async def due(self, now: datetime) -> list[Timer]:
        found = await self._s.all(
            "SELECT t.timer_id, t.load_id, t.obligation_id, t.due_at, t.created_at, t.status,"
            " t.reason"
            " FROM timers t JOIN obligations o ON o.obligation_id = t.obligation_id"
            " WHERE t.status = %s AND t.due_at <= %s"
            " ORDER BY t.due_at, id_order(t.timer_id), t.timer_id"
            " FOR NO KEY UPDATE OF t, o SKIP LOCKED",
            (_PENDING, now),
        )
        return [rows.timer_from(row) for row in found]

    async def next_due_at(self) -> datetime | None:
        row = await self._s.one(
            "SELECT min(due_at) AS due_at FROM timers WHERE status = %s", (_PENDING,)
        )
        return None if row is None else row["due_at"]

    async def pending_for_obligation(self, obligation_id: str) -> list[Timer]:
        found = await self._s.all(
            f"SELECT {_TIMER} FROM timers WHERE obligation_id = %s AND status = %s"
            " ORDER BY due_at, id_order(timer_id), timer_id",
            (obligation_id, _PENDING),
        )
        return [rows.timer_from(row) for row in found]

    async def list_for_load(self, load_id: str) -> list[Timer]:
        found = await self._s.all(
            f"SELECT {_TIMER} FROM timers WHERE load_id = %s"
            " ORDER BY created_at, id_order(timer_id), timer_id",
            (load_id,),
        )
        return [rows.timer_from(row) for row in found]


class PostgresOutboxRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    _VALUES: Final = (
        "%(outbox_id)s, %(idempotency_key)s, %(run_id)s, %(load_id)s, %(kind)s, %(payload)s,"
        " %(created_at)s, %(status)s, %(attempts)s, %(last_error)s, %(accepted_at)s"
    )

    async def add(self, item: OutboxItem) -> bool:
        row = await self._s.one(
            f"INSERT INTO outbox ({_OUTBOX}) VALUES ({self._VALUES})"
            " ON CONFLICT (idempotency_key) DO NOTHING RETURNING outbox_id",
            rows.outbox_params(item),
        )
        return row is not None

    async def pending(self, limit: int = 100) -> list[OutboxItem]:
        if limit <= 0:
            return []
        found = await self._s.all(
            f"SELECT {_OUTBOX} FROM outbox WHERE status = %s"
            " ORDER BY created_at, id_order(outbox_id), seq LIMIT %s",
            (OutboxStatus.PENDING.value, limit),
        )
        return [rows.outbox_from(row) for row in found]

    async def save(self, item: OutboxItem) -> None:
        await self._s.run(
            f"INSERT INTO outbox ({_OUTBOX}) VALUES ({self._VALUES})"
            " ON CONFLICT (outbox_id) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key,"
            " run_id = EXCLUDED.run_id, load_id = EXCLUDED.load_id, kind = EXCLUDED.kind,"
            " payload = EXCLUDED.payload, created_at = EXCLUDED.created_at,"
            " status = EXCLUDED.status, attempts = EXCLUDED.attempts,"
            " last_error = EXCLUDED.last_error, accepted_at = EXCLUDED.accepted_at",
            rows.outbox_params(item),
        )

    async def list_for_load(self, load_id: str) -> list[OutboxItem]:
        found = await self._s.all(
            f"SELECT {_OUTBOX} FROM outbox WHERE load_id = %s"
            " ORDER BY created_at, id_order(outbox_id), seq",
            (load_id,),
        )
        return [rows.outbox_from(row) for row in found]


class PostgresRunRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def save(self, run: RunRecord) -> None:
        await self._s.run(
            "INSERT INTO runs (run_id, event_id, load_id, started_at, status, record)"
            " VALUES (%(run_id)s, %(event_id)s, %(load_id)s, %(started_at)s, %(status)s,"
            " %(record)s)"
            " ON CONFLICT (run_id) DO UPDATE SET event_id = EXCLUDED.event_id,"
            " load_id = EXCLUDED.load_id, started_at = EXCLUDED.started_at,"
            " status = EXCLUDED.status, record = EXCLUDED.record",
            rows.run_params(run),
        )

    async def get(self, run_id: str) -> RunRecord | None:
        row = await self._s.one("SELECT record FROM runs WHERE run_id = %s", (run_id,))
        return None if row is None else rows.run_from(row)

    async def for_event(self, event_id: str) -> RunRecord | None:
        # The last of the event's runs in `list_for_load` order.
        row = await self._s.one(
            "SELECT record FROM runs WHERE event_id = %s"
            " ORDER BY started_at DESC, id_order(run_id) DESC, run_id DESC LIMIT 1",
            (event_id,),
        )
        return None if row is None else rows.run_from(row)

    async def list_for_load(self, load_id: str) -> list[RunRecord]:
        found = await self._s.all(
            "SELECT record FROM runs WHERE load_id = %s"
            " ORDER BY started_at, id_order(run_id), run_id",
            (load_id,),
        )
        return [rows.run_from(row) for row in found]


# The oldest queued event whose load has nothing in flight. Only the head of a load's
# queue qualifies (no older event of the load still queued), so a worker that skips a
# head another worker has locked does not take the event behind it instead.
_CLAIM: Final = f"""
WITH next AS (
    SELECT q.event_id
    FROM event_queue q
    WHERE q.state = 'queued'
      AND (%(load_id)s::text IS NULL OR q.load_id = %(load_id)s)
      AND (%(event_id)s::text IS NULL OR q.event_id = %(event_id)s)
      AND NOT EXISTS (
          SELECT 1 FROM event_queue busy
          WHERE busy.load_id = q.load_id AND busy.state = 'in_flight'
      )
      AND NOT EXISTS (
          SELECT 1 FROM event_queue ahead
          WHERE ahead.load_id = q.load_id AND ahead.state = 'queued' AND ahead.seq < q.seq
      )
    ORDER BY q.seq
    LIMIT 1
    FOR NO KEY UPDATE OF q SKIP LOCKED
), claimed AS (
    UPDATE event_queue q SET state = 'in_flight', claimed_at = %(now)s, claim_token = %(token)s
    FROM next WHERE q.event_id = next.event_id
    RETURNING q.event_id
)
SELECT {_EVENT_E} FROM events e JOIN claimed ON claimed.event_id = e.event_id
"""

# A claim that loses a race (see `claim_next`) tries again this many times in all.
_CLAIM_ATTEMPTS: Final = 3

# Releases the claims older than the cutoff, as `release` does. A row another unit of
# work holds is skipped: that worker is completing or releasing it right now.
_RECLAIM: Final = """
WITH expired AS (
    SELECT event_id FROM event_queue
    WHERE state = 'in_flight' AND claimed_at < %(cutoff)s
    FOR NO KEY UPDATE SKIP LOCKED
)
UPDATE event_queue q SET failures = q.failures + 1, last_error = %(error)s, claimed_at = NULL,
    state = CASE WHEN q.failures + 1 >= %(max)s THEN 'dead' ELSE 'queued' END
FROM expired WHERE q.event_id = expired.event_id
RETURNING q.event_id
"""


class PostgresEventQueue:
    """The per-load FIFO in `event_queue`, ordered by arrival (`seq`).

    `claim_next` locks the head it takes with `SKIP LOCKED`, so workers on separate
    connections never wait for each other and never take the same event. One race is
    left: arrival order is taken when an event is inserted and becomes visible when it
    commits, so a worker can see an older event of a load whose newer event another
    worker has just claimed and not yet committed. The partial unique index
    `event_queue_one_in_flight_per_load` decides it: the second claim waits for the
    first and fails; it rolls back to its savepoint and looks again, and then sees the
    load in flight. The index is the guarantee; the locking keeps the race rare.
    """

    def __init__(self, session: Session, max_attempts: int) -> None:
        self._s = session
        self._max_attempts = max_attempts

    async def enqueue(self, event: Event) -> None:
        """Logs the event too if it is not logged yet. Enqueuing an event that is
        already queued, in flight or dead changes nothing."""
        await self._s.run(
            f"INSERT INTO events ({_EVENT}) VALUES ({_EVENT_VALUES})"
            " ON CONFLICT (event_id) DO NOTHING",
            rows.event_params(event),
        )
        await self._s.run(
            "INSERT INTO event_queue (event_id, load_id) VALUES (%s, %s)"
            " ON CONFLICT (event_id) DO NOTHING",
            (event.event_id, event.load_id),
        )

    async def claim_next(
        self,
        now: datetime,
        *,
        load_id: str | None = None,
        event_id: str | None = None,
        token: str | None = None,
    ) -> Event | None:
        """`claimed_at` is `now`, the runtime's clock, not the database's: the lease
        compares it with the runtime's `now` (see `reclaim_expired`)."""
        params = {
            "now": rows.aware(now, "claim time"),
            "load_id": load_id,
            "event_id": event_id,
            "token": token,
        }
        for _ in range(_CLAIM_ATTEMPTS):
            try:
                async with self._s.conn.transaction():  # a savepoint in the unit of work
                    row = await self._s.one(_CLAIM, params)
            except errors.UniqueViolation:
                continue  # another worker took this load first; the next look sees it
            return None if row is None else rows.event_from(row)
        return None

    async def owns_claim(self, event_id: str, token: str) -> bool:
        row = await self._s.one(
            "SELECT event_id FROM event_queue WHERE event_id = %s"
            " AND state = 'in_flight' AND claim_token = %s FOR NO KEY UPDATE",
            (event_id, token),
        )
        return row is not None

    async def head(self, load_id: str) -> Event | None:
        row = await self._s.one(
            f"SELECT {_EVENT_E} FROM event_queue q JOIN events e USING (event_id)"
            " WHERE q.load_id = %s ORDER BY q.seq LIMIT 1",
            (load_id,),
        )
        return None if row is None else rows.event_from(row)

    async def is_dead(self, event_id: str) -> bool:
        return (
            await self._s.one(
                "SELECT event_id FROM event_queue WHERE event_id = %s AND state = 'dead'",
                (event_id,),
            )
            is not None
        )

    async def wake_loads(self, *, include_timers: bool = False) -> list[str]:
        found = await self._s.all(
            "SELECT load_id FROM ("
            " SELECT load_id FROM event_queue WHERE state IN ('queued', 'in_flight')"
            " UNION SELECT load_id FROM outbox WHERE status = 'pending'"
            " UNION SELECT load_id FROM timers WHERE %s AND status = 'pending'"
            ") active WHERE NOT EXISTS ("
            " SELECT 1 FROM event_queue q WHERE q.load_id = active.load_id AND q.state = 'dead'"
            ") AND NOT EXISTS ("
            " SELECT 1 FROM outbox o WHERE o.load_id = active.load_id AND o.status = 'failed'"
            ") ORDER BY load_id",
            (include_timers,),
        )
        return [row["load_id"] for row in found]

    async def complete(self, event_id: str) -> None:
        await self._s.run(
            "DELETE FROM event_queue WHERE event_id = %s AND state = 'in_flight'", (event_id,)
        )

    async def release(self, event_id: str, error: str) -> None:
        await self._s.run(
            "UPDATE event_queue SET failures = failures + 1, last_error = %(error)s,"
            " claimed_at = NULL,"
            " state = CASE WHEN failures + 1 >= %(max)s THEN 'dead' ELSE 'queued' END"
            " WHERE event_id = %(event_id)s AND state = 'in_flight'",
            {"event_id": event_id, "error": error, "max": self._max_attempts},
        )

    async def reclaim_expired(self, now: datetime, lease_s: float) -> int:
        cutoff = rows.aware(now, "reclaim time") - timedelta(seconds=lease_s)
        params = {
            "cutoff": cutoff,
            "error": f"claim lease of {lease_s:g} s expired",
            "max": self._max_attempts,
        }
        return len(await self._s.all(_RECLAIM, params))

    async def size(self) -> int:
        row = await self._s.one("SELECT count(*) AS n FROM event_queue WHERE state = 'queued'")
        return 0 if row is None else int(row["n"])

    async def dead_letters(self) -> list[Event]:
        """In arrival order."""
        found = await self._s.all(
            f"SELECT {_EVENT_E} FROM event_queue q JOIN events e ON e.event_id = q.event_id"
            " WHERE q.state = 'dead' ORDER BY q.seq"
        )
        return [rows.event_from(row) for row in found]
