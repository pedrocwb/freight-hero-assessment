"""In-memory persistence: unit tests, evals, Studio worlds, and the platform by default
(`PERSISTENCE=memory`; `adapters/persistence/postgres/` is the durable store).

Transactions are serialized with a lock and rolled back by restoring a snapshot,
so a failed run leaves no partial state, exactly like a database transaction.

The wave 3 document repository lives in its own module (`documents.py`), over the fields
of `_State` declared here once: its card changes that module, never this file.
"""

import asyncio
import bisect
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import TracebackType
from typing import Self

from freight_agent.adapters.persistence.memory.documents import MemoryDocumentRepository
from freight_agent.domain.documents import Document
from freight_agent.domain.events import Event
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.outbox import OutboxItem, OutboxStatus
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.interfaces.documents import DocumentRepository
from freight_agent.interfaces.repositories import (
    EventLog,
    EventQueue,
    LoadRepository,
    ObligationRepository,
    OutboxRepository,
    RunRepository,
    TimerRepository,
)


def _id_key(value: str) -> tuple[str, int]:
    """Natural order for ids such as `out_9` < `out_10`: ties on time are broken by id,
    and plain text order would deliver the tenth action of a run before the ninth."""
    prefix, _, number = value.rpartition("_")
    return (prefix, int(number)) if number.isdigit() else (value, -1)


@dataclass
class _State:
    loads: dict[str, Load] = field(default_factory=dict)
    trackers: dict[str, RiskTracker] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)
    event_order: list[str] = field(default_factory=list)
    obligations: dict[str, Obligation] = field(default_factory=dict)
    timers: dict[str, Timer] = field(default_factory=dict)
    outbox: dict[str, OutboxItem] = field(default_factory=dict)
    outbox_keys: set[str] = field(default_factory=set)
    runs: dict[str, RunRecord] = field(default_factory=dict)
    queue: list[str] = field(default_factory=list)  # waiting event ids, in arrival order
    arrivals: dict[str, int] = field(default_factory=dict)  # event_id -> arrival number
    next_arrival: int = 0
    inflight: dict[str, str] = field(default_factory=dict)  # load_id -> event_id
    claimed_at: dict[str, datetime] = field(default_factory=dict)  # in-flight event_id -> claim
    claim_tokens: dict[str, str | None] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    dead: list[str] = field(default_factory=list)
    # Wave 3 (`documents.py`).
    documents: dict[str, Document] = field(default_factory=dict)


class MemoryStore:
    def __init__(self, max_queue_attempts: int = 3) -> None:
        self.state = _State()
        self.max_queue_attempts = max_queue_attempts
        self.lock = asyncio.Lock()

    def unit_of_work(self) -> "MemoryUnitOfWork":
        return MemoryUnitOfWork(self)

    async def clear(self) -> None:
        """Forget everything: a fresh store, as `PostgresStore.clear` empties its tables."""
        async with self.lock:
            self.state = _State()


class MemoryLoadRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def add(self, load: Load) -> bool:
        if load.load_id in self._s.state.loads:
            return False
        self._s.state.loads[load.load_id] = load
        return True

    async def get(self, load_id: str) -> Load | None:
        return self._s.state.loads.get(load_id)

    async def save(self, load: Load) -> None:
        self._s.state.loads[load.load_id] = load

    async def list_ids(self) -> list[str]:
        return sorted(self._s.state.loads)

    async def get_tracker(self, load_id: str) -> RiskTracker | None:
        return self._s.state.trackers.get(load_id)

    async def save_tracker(self, load_id: str, tracker: RiskTracker) -> None:
        self._s.state.trackers[load_id] = tracker


class MemoryEventLog:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def append(self, event: Event) -> bool:
        state = self._s.state
        if event.event_id in state.events:
            return False
        state.events[event.event_id] = event
        state.event_order.append(event.event_id)
        return True

    async def get(self, event_id: str) -> Event | None:
        return self._s.state.events.get(event_id)

    async def recent(self, load_id: str, limit: int) -> list[Event]:
        state = self._s.state
        matching = [
            state.events[i] for i in state.event_order if state.events[i].load_id == load_id
        ]
        return matching[-limit:] if limit > 0 else []


class MemoryObligationRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def get(self, obligation_id: str) -> Obligation | None:
        return self._s.state.obligations.get(obligation_id)

    async def get_open(self, load_id: str, kind: str) -> Obligation | None:
        return next(
            (
                o
                for o in self._s.state.obligations.values()
                if o.load_id == load_id and o.kind == kind and o.is_open
            ),
            None,
        )

    async def list_for_load(self, load_id: str) -> list[Obligation]:
        items = [o for o in self._s.state.obligations.values() if o.load_id == load_id]
        # The id breaks ties, as in every other list: a database has no insertion order.
        return sorted(items, key=lambda o: (o.opened_at, _id_key(o.obligation_id)))

    async def save(self, obligation: Obligation) -> None:
        self._s.state.obligations[obligation.obligation_id] = obligation


class MemoryTimerRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def get(self, timer_id: str) -> Timer | None:
        return self._s.state.timers.get(timer_id)

    async def save(self, timer: Timer) -> None:
        self._s.state.timers[timer.timer_id] = timer

    def _pending(self) -> list[Timer]:
        pending = [t for t in self._s.state.timers.values() if t.status is TimerStatus.PENDING]
        return sorted(pending, key=lambda t: (t.due_at, _id_key(t.timer_id)))

    async def due(self, now: datetime) -> list[Timer]:
        return [t for t in self._pending() if t.due_at <= now]

    async def next_due_at(self) -> datetime | None:
        pending = self._pending()
        return pending[0].due_at if pending else None

    async def pending_for_obligation(self, obligation_id: str) -> list[Timer]:
        return [t for t in self._pending() if t.obligation_id == obligation_id]

    async def list_for_load(self, load_id: str) -> list[Timer]:
        items = [t for t in self._s.state.timers.values() if t.load_id == load_id]
        return sorted(items, key=lambda t: (t.created_at, _id_key(t.timer_id)))


class MemoryOutboxRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def add(self, item: OutboxItem) -> bool:
        state = self._s.state
        if item.idempotency_key in state.outbox_keys:
            return False
        state.outbox_keys.add(item.idempotency_key)
        state.outbox[item.outbox_id] = item
        return True

    async def pending(self, limit: int = 100) -> list[OutboxItem]:
        items = [i for i in self._s.state.outbox.values() if i.status is OutboxStatus.PENDING]
        return sorted(items, key=lambda i: (i.created_at, _id_key(i.outbox_id)))[:limit]

    async def save(self, item: OutboxItem) -> None:
        self._s.state.outbox[item.outbox_id] = item

    async def list_for_load(self, load_id: str) -> list[OutboxItem]:
        items = [i for i in self._s.state.outbox.values() if i.load_id == load_id]
        return sorted(items, key=lambda i: (i.created_at, _id_key(i.outbox_id)))


class MemoryRunRepository:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def save(self, run: RunRecord) -> None:
        self._s.state.runs[run.run_id] = run

    async def get(self, run_id: str) -> RunRecord | None:
        return self._s.state.runs.get(run_id)

    async def for_event(self, event_id: str) -> RunRecord | None:
        matching = [r for r in self._s.state.runs.values() if r.event_id == event_id]
        # The last of the event's runs in `list_for_load` order, ties included.
        return max(matching, key=lambda r: (r.started_at, _id_key(r.run_id))) if matching else None

    async def list_for_load(self, load_id: str) -> list[RunRecord]:
        items = [r for r in self._s.state.runs.values() if r.load_id == load_id]
        return sorted(items, key=lambda r: (r.started_at, _id_key(r.run_id)))


class MemoryEventQueue:
    def __init__(self, store: MemoryStore) -> None:
        self._s = store

    async def enqueue(self, event: Event) -> None:
        state = self._s.state
        if event.event_id not in state.events:
            state.events[event.event_id] = event
            state.event_order.append(event.event_id)
        if (
            event.event_id in state.queue
            or event.event_id in state.inflight.values()
            or event.event_id in state.dead
        ):
            return
        state.queue.append(event.event_id)
        state.arrivals[event.event_id] = state.next_arrival
        state.next_arrival += 1

    async def claim_next(
        self,
        now: datetime,
        *,
        load_id: str | None = None,
        event_id: str | None = None,
        token: str | None = None,
    ) -> Event | None:
        state = self._s.state
        seen: set[str] = set()
        for index, queued_id in enumerate(state.queue):
            event = state.events[queued_id]
            if event.load_id in seen:
                continue
            seen.add(event.load_id)
            if load_id is not None and event.load_id != load_id:
                continue
            if event_id is not None and queued_id != event_id:
                continue
            if event.load_id not in state.inflight:
                del state.queue[index]
                state.inflight[event.load_id] = queued_id
                state.claimed_at[queued_id] = now
                state.claim_tokens[queued_id] = token
                return event
        return None

    async def owns_claim(self, event_id: str, token: str) -> bool:
        return self._in_flight(event_id) and self._s.state.claim_tokens.get(event_id) == token

    async def head(self, load_id: str) -> Event | None:
        state = self._s.state
        ids = [
            i
            for i in [*state.queue, *state.inflight.values(), *state.dead]
            if state.events[i].load_id == load_id
        ]
        return state.events[min(ids, key=state.arrivals.__getitem__)] if ids else None

    async def is_dead(self, event_id: str) -> bool:
        return event_id in self._s.state.dead

    async def wake_loads(self, *, include_timers: bool = False) -> list[str]:
        state = self._s.state
        active = {state.events[i].load_id for i in [*state.queue, *state.inflight.values()]}
        active |= {i.load_id for i in state.outbox.values() if i.status is OutboxStatus.PENDING}
        if include_timers:
            active |= {t.load_id for t in state.timers.values() if t.status is TimerStatus.PENDING}
        blocked = {state.events[i].load_id for i in state.dead}
        blocked |= {i.load_id for i in state.outbox.values() if i.status is OutboxStatus.FAILED}
        return sorted(active - blocked)

    async def complete(self, event_id: str) -> None:
        if self._in_flight(event_id):
            self._end_claim(event_id)

    async def release(self, event_id: str, error: str) -> None:
        if not self._in_flight(event_id):
            return  # completed, dead, or reclaimed after its lease: nothing to give back
        self._end_claim(event_id)
        state = self._s.state
        state.failures[event_id] = state.failures.get(event_id, 0) + 1
        if state.failures[event_id] >= self._s.max_queue_attempts:
            state.dead.append(event_id)
        else:
            # Back to its place by arrival: ahead of its load's later events, and still
            # behind older events of other loads, so a failure never jumps the queue.
            bisect.insort(state.queue, event_id, key=state.arrivals.__getitem__)

    async def reclaim_expired(self, now: datetime, lease_s: float) -> int:
        cutoff = now - timedelta(seconds=lease_s)
        expired = [e for e, claimed in self._s.state.claimed_at.items() if claimed < cutoff]
        for event_id in expired:
            await self.release(event_id, f"claim lease of {lease_s:g} s expired")
        return len(expired)

    def _in_flight(self, event_id: str) -> bool:
        state = self._s.state
        event = state.events.get(event_id)
        return event is not None and state.inflight.get(event.load_id) == event_id

    def _end_claim(self, event_id: str) -> None:
        state = self._s.state
        del state.inflight[state.events[event_id].load_id]
        del state.claimed_at[event_id]
        state.claim_tokens.pop(event_id, None)

    async def size(self) -> int:
        return len(self._s.state.queue)

    async def dead_letters(self) -> list[Event]:
        return [self._s.state.events[i] for i in self._s.state.dead]


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self._snapshot: _State | None = None
        self._committed = False
        # Typed as the interfaces: the protocol's attributes are invariant, so a concrete
        # annotation would keep this unit of work from satisfying `UnitOfWork`.
        self.loads: LoadRepository = MemoryLoadRepository(store)
        self.events: EventLog = MemoryEventLog(store)
        self.obligations: ObligationRepository = MemoryObligationRepository(store)
        self.timers: TimerRepository = MemoryTimerRepository(store)
        self.outbox: OutboxRepository = MemoryOutboxRepository(store)
        self.runs: RunRepository = MemoryRunRepository(store)
        self.queue: EventQueue = MemoryEventQueue(store)
        self.documents: DocumentRepository = MemoryDocumentRepository(store)

    async def __aenter__(self) -> Self:
        await self._store.lock.acquire()
        self._snapshot = copy.deepcopy(self._store.state)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None or not self._committed:
                assert self._snapshot is not None
                self._store.state = self._snapshot
        finally:
            self._snapshot = None
            self._store.lock.release()

    async def commit(self) -> None:
        self._committed = True
