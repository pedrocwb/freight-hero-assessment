"""Persistence ports, implemented by the memory store (`adapters/persistence/memory/`)."""

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from freight_agent.domain.events import Event
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import Timer
from freight_agent.interfaces.documents import DocumentRepository


class LoadRepository(Protocol):
    async def get(self, load_id: str) -> Load | None: ...
    async def save(self, load: Load) -> None: ...
    async def list_ids(self) -> list[str]: ...
    async def get_tracker(self, load_id: str) -> RiskTracker | None: ...
    async def save_tracker(self, load_id: str, tracker: RiskTracker) -> None: ...


class EventLog(Protocol):
    async def append(self, event: Event) -> bool:
        """Append the event. Returns False, and stores nothing, if `event_id` already exists."""
        ...

    async def get(self, event_id: str) -> Event | None: ...

    async def recent(self, load_id: str, limit: int) -> list[Event]:
        """The last `limit` events of the load, oldest first."""
        ...


class ObligationRepository(Protocol):
    async def get(self, obligation_id: str) -> Obligation | None: ...
    async def get_open(self, load_id: str, kind: str) -> Obligation | None: ...
    async def list_for_load(self, load_id: str) -> list[Obligation]: ...
    async def save(self, obligation: Obligation) -> None: ...


class TimerRepository(Protocol):
    async def get(self, timer_id: str) -> Timer | None: ...
    async def save(self, timer: Timer) -> None: ...

    async def due(self, now: datetime) -> list[Timer]:
        """Pending timers with `due_at <= now`, earliest first."""
        ...

    async def next_due_at(self) -> datetime | None: ...
    async def pending_for_obligation(self, obligation_id: str) -> list[Timer]: ...
    async def list_for_load(self, load_id: str) -> list[Timer]: ...


class OutboxRepository(Protocol):
    async def add(self, item: OutboxItem) -> bool:
        """Store the item. Returns False, and stores nothing, if the idempotency key exists."""
        ...

    async def pending(self, limit: int = 100) -> list[OutboxItem]: ...
    async def save(self, item: OutboxItem) -> None: ...
    async def list_for_load(self, load_id: str) -> list[OutboxItem]: ...


class RunRepository(Protocol):
    async def save(self, run: RunRecord) -> None: ...
    async def get(self, run_id: str) -> RunRecord | None: ...
    async def for_event(self, event_id: str) -> RunRecord | None: ...
    async def list_for_load(self, load_id: str) -> list[RunRecord]: ...


class EventQueue(Protocol):
    """Per-load FIFO. At most one event per load is in flight at a time.

    Every time given is the runtime's clock, never the store's: the eval harness and the
    simulation run on a virtual clock.
    """

    async def enqueue(self, event: Event) -> None: ...

    async def claim_next(self, now: datetime) -> Event | None:
        """The oldest queued event whose load has nothing in flight, or None.

        The event is in flight from `now`, the claim time `reclaim_expired` counts from.
        """
        ...

    async def complete(self, event_id: str) -> None:
        """Drop an event in flight from the queue. An event not in flight is left as it is."""
        ...

    async def release(self, event_id: str, error: str) -> None:
        """Return an event in flight to the queue, counted as a failed attempt; after too
        many it becomes a dead letter. An event not in flight is left as it is."""
        ...

    async def reclaim_expired(self, now: datetime, lease_s: float) -> int:
        """Release every event claimed more than `lease_s` seconds before `now`.

        A worker that dies between claim and commit (a crash, a cancelled run) never
        completes or releases its event, and the load's later events would wait forever.
        Each reclaim counts as a failed attempt, as `release` does, so an event whose runs
        always die still becomes a dead letter. Returns how many events it released.
        """
        ...

    async def size(self) -> int: ...

    async def dead_letters(self) -> list[Event]: ...


class UnitOfWork(Protocol):
    """One transaction. Leaving the context without `commit()` rolls everything back."""

    loads: LoadRepository
    events: EventLog
    obligations: ObligationRepository
    timers: TimerRepository
    outbox: OutboxRepository
    runs: RunRepository
    queue: EventQueue
    documents: DocumentRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
