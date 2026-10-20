"""What one run may read, loaded once at the start of the run.

Every later step (prefilter, gate, SOP resolution, decide, validate, execute)
reads this snapshot instead of querying the repositories again, so a run sees
one consistent view of the load's memory.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from freight_agent.domain.broker import Broker, choose_version, standard_broker
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import RUN_TRIGGERS, Event, EventType
from freight_agent.domain.load import Channel, Load, Role
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.outbox import OutboxKind
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import OutboundRecord
from freight_agent.ports.repositories import EventLog, UnitOfWork


@dataclass(frozen=True)
class RunContext:
    event: Event
    load: Load
    now: datetime
    obligations: tuple[Obligation, ...]  # every obligation of the load, oldest first
    pending_timers: tuple[Timer, ...]  # earliest first
    # The last N events of the load, oldest first, without the run triggers logged after
    # `event` (they get their own runs); every agent action is kept (docs/decisions.md).
    recent_events: tuple[Event, ...]
    recent_outbound: tuple[OutboundRecord, ...]  # SMS, email and Slack sent in the rate window
    event_timer: Timer | None = None  # for TIMER_DUE events: the timer named in `event.data`
    tracker: RiskTracker | None = None
    already_processed: bool = False  # a run record already exists for this event
    # The catalog and limits this run follows: the load's broker at the version its
    # rollout gives this load, or the standard catalog for a broker never saved.
    broker: Broker = field(default_factory=standard_broker)

    @property
    def open_obligations(self) -> tuple[Obligation, ...]:
        return tuple(o for o in self.obligations if o.is_open)

    def open_obligation(self, kind: str) -> Obligation | None:
        return next((o for o in self.open_obligations if o.kind == kind), None)

    def obligation(self, obligation_id: str) -> Obligation | None:
        return next((o for o in self.obligations if o.obligation_id == obligation_id), None)


async def load_run_context(
    uow: UnitOfWork,
    event: Event,
    *,
    now: datetime,
    recent_limit: int,
    outbound_window_minutes: int | None = None,
) -> RunContext:
    """Read the load, its broker, ledger, timers, recent events and recent outbound messages.

    `outbound_window_minutes` defaults to the broker's SMS rate window. Raises
    `DomainError` when the event names a load that was never registered: there is no
    SOP, contact list or ledger to act on.
    """
    load = await uow.loads.get(event.load_id)
    if load is None:
        raise DomainError(f"unknown load {event.load_id!r} for event {event.event_id!r}")
    broker = await _broker_for(uow, load)
    if outbound_window_minutes is None:
        outbound_window_minutes = broker.limits.validation.sms_window_minutes

    obligations = sorted(
        await uow.obligations.list_for_load(load.load_id),
        key=lambda o: (o.opened_at, o.obligation_id),
    )
    timers = await uow.timers.list_for_load(load.load_id)
    pending = sorted(
        (t for t in timers if t.status is TimerStatus.PENDING),
        key=lambda t: (t.due_at, t.timer_id),
    )
    recent = await uow.events.recent(load.load_id, recent_limit)
    since = now - timedelta(minutes=outbound_window_minutes)
    outbound_scan = await _events_since(uow.events, load.load_id, since, recent, recent_limit)

    return RunContext(
        event=event,
        load=load,
        now=now,
        obligations=tuple(obligations),
        pending_timers=tuple(pending),
        recent_events=await _history_until(uow.events, event, recent, recent_limit),
        recent_outbound=_outbound_records(outbound_scan, load, since),
        event_timer=await _event_timer(uow, event),
        tracker=await uow.loads.get_tracker(load.load_id),
        already_processed=await uow.runs.for_event(event.event_id) is not None,
        broker=broker,
    )


async def _broker_for(uow: UnitOfWork, load: Load) -> Broker:
    """The load's broker, at the version its rollout gives this load (`choose_version`).

    A broker never saved runs on the standard catalog, so loads of brokers onboarded
    before brokers were data behave as they always did.
    """
    current = await uow.brokers.get(load.broker_id)
    if current is None:
        return standard_broker()
    rollout = current.rollout
    previous = None
    if rollout.percent < 100 and rollout.previous_version is not None:
        previous = await uow.brokers.get_version(current.broker_id, rollout.previous_version)
    return choose_version(current, previous, load.load_id)


async def _event_timer(uow: UnitOfWork, event: Event) -> Timer | None:
    """The timer a TIMER_DUE event names. None when it names none or it no longer exists."""
    if event.type is not EventType.TIMER_DUE:
        return None
    timer_id = event.data.get("timer_id")
    if not isinstance(timer_id, str):
        return None
    return await uow.timers.get(timer_id)


async def _history_until(
    events: EventLog, event: Event, page: Sequence[Event], limit: int
) -> tuple[Event, ...]:
    """The last `limit` events of the load, in log order, without the run triggers
    logged after `event`.

    Events that arrived while this event waited in the per-load queue are already in
    the log. They get their own runs, and the model must not read them as history: it
    would act on an ETA that, for this run, has not arrived yet. The agent's own actions
    are always kept, wherever they fall: an earlier event's run may commit its reply
    after this event was logged, and that reply is what the agent has really done.

    `page` is the newest `limit` events. It grows while it does not hold `event` or holds
    fewer than `limit` events to keep, up to the whole history; when `event` is not
    logged at all, the result is the latest events.
    """
    if limit <= 0:
        return ()
    size = limit
    while True:
        whole = len(page) < size  # the page holds every event of the load
        kept = _without_later_triggers(page, event.event_id)
        if kept is not None and (len(kept) >= limit or whole):
            return tuple(kept[-limit:])
        if kept is None and whole:
            return tuple(page[-limit:])
        size *= 2
        page = await events.recent(event.load_id, size)


def _without_later_triggers(page: Sequence[Event], event_id: str) -> list[Event] | None:
    """`page` up to and including the event, then only the agent actions after it.
    None when the event is not in `page`."""
    ids = [e.event_id for e in page]
    if event_id not in ids:
        return None
    cut = ids.index(event_id) + 1
    return [*page[:cut], *(e for e in page[cut:] if e.type not in RUN_TRIGGERS)]


async def _events_since(
    events: EventLog, load_id: str, since: datetime, page: Sequence[Event], size: int
) -> Sequence[Event]:
    """The newest events of the load, enough of them to hold every agent action since `since`.

    `recent_limit` is sized for the model's prompt, not for the rate window: a burst
    of chatter could push an SMS of the last hour out of the page, and the rate limit
    would never count it. So the page grows until it reaches an agent action older
    than the window, or the whole history. Agent actions are logged in run order with
    the run's clock time, so every agent action before that one is older too. One
    query is enough in the usual case, because the first page is the one already read.
    """
    while len(page) >= size and not _reaches_before(page, since):
        size = max(2 * size, 1)
        page = await events.recent(load_id, size)
    return page


def _reaches_before(page: Sequence[Event], since: datetime) -> bool:
    return any(_is_agent_action(e) and e.occurred_at < since for e in page)


def _is_agent_action(event: Event) -> bool:
    return event.type is EventType.AGENT_ACTION


def _outbound_records(
    events: Sequence[Event], load: Load, since: datetime
) -> tuple[OutboundRecord, ...]:
    """One record per recipient of each SMS, email and broker Slack post since `since`.

    The window has no upper bound: an action stamped after `now` (clock skew) still
    counts, and over-counting only makes the rate limit stricter.
    """
    return tuple(
        OutboundRecord(contact_id=contact_id, channel=channel, at=event.occurred_at)
        for event in events
        if _is_agent_action(event) and event.occurred_at >= since
        for contact_id, channel in _recipients(event, load)
    )


def _recipients(event: Event, load: Load) -> tuple[tuple[str, Channel], ...]:
    """Who an agent action reached, read from its outbox payload (docs/decisions.md).

    A malformed payload yields nothing instead of failing the run: a bad log entry
    must not block every later run of the load.
    """
    kind: object = event.data.get("kind")
    raw: object = event.data.get("payload")
    payload: Mapping[str, object] = (
        cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
    )
    if kind == OutboxKind.SMS:
        return tuple((c, Channel.SMS) for c in _strings([payload.get("contact_id")]))
    if kind == OutboxKind.EMAIL:
        return tuple((c, Channel.EMAIL) for c in _strings(payload.get("to_contact_ids")))
    if kind == OutboxKind.BROKER_SLACK:
        return tuple((c, Channel.SLACK) for c in _strings([_broker_on_slack(load)]))
    return ()


def _broker_on_slack(load: Load) -> str | None:
    """The contact a broker Slack post counts against.

    The post goes to the broker's channel, not to a person, so it is attributed to
    the first broker contact that is on Slack: one record per post keeps counts by
    channel exact. A load with no broker on Slack records nothing.
    """
    brokers = load.contacts_with_role(Role.BROKER)
    return next((c.id for c in brokers if Channel.SLACK in c.channels), None)


def _strings(value: object) -> tuple[str, ...]:
    """The strings in a list or tuple. Anything else, or any other item, is dropped."""
    if not isinstance(value, list | tuple):
        return ()
    items = cast(Sequence[object], value)
    return tuple(item for item in items if isinstance(item, str))
