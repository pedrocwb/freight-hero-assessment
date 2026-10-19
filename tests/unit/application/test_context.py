from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.persistence.memory.store import MemoryStore, MemoryUnitOfWork
from freight_agent.application.context import RunContext, load_run_context
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Direction, Event, EventType
from freight_agent.domain.load import Channel, Contact, DelayRisk, Load, Role
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import OutboundRecord
from freight_agent.ports.repositories import UnitOfWork

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)
NOW = T0 + timedelta(minutes=90)
WINDOW = 60
OTHER = "999999"


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _message(
    event_id: str, *, minutes: float = 0, load_id: str = "481207", body: str = "ok"
) -> Event:
    return Event(
        event_id=event_id,
        load_id=load_id,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=_at(minutes),
        channel=Channel.SMS,
        sender_contact_id="drv_20931",
        body=body,
    )


def _action(
    key: str,
    kind: str,
    payload: dict[str, Any],
    *,
    at: datetime,
    load_id: str = "481207",
    channel: Channel = Channel.SMS,
) -> Event:
    """An `agent_action` event, shaped as the executor logs it (docs/decisions.md)."""
    return Event(
        event_id=f"act:{key}",
        load_id=load_id,
        type=EventType.AGENT_ACTION,
        occurred_at=at,
        channel=channel,
        direction=Direction.OUTBOUND,
        body=str(payload.get("text", "")),
        data={"kind": kind, "payload": payload, "run_id": "run_1", "outbox_id": f"out_{key}"},
    )


def _sms(
    key: str, *, at: datetime, contact_id: str = "drv_20931", load_id: str = "481207"
) -> Event:
    payload = {"contact_id": contact_id, "text": "ETA?", "purpose": "eta_request"}
    return _action(key, "sms", payload, at=at, load_id=load_id)


def _timer_due(timer_id: str, *, load_id: str = "481207") -> Event:
    return Event(
        event_id=f"timer:{timer_id}",
        load_id=load_id,
        type=EventType.TIMER_DUE,
        occurred_at=NOW,
        channel=Channel.SYSTEM,
        data={"timer_id": timer_id, "obligation_id": "obl_1", "kind": "eta_request"},
    )


def _obligation(obligation_id: str, *, minutes: float, load_id: str = "481207") -> Obligation:
    return Obligation.open(
        obligation_id=obligation_id,
        load_id=load_id,
        kind=ObligationKind.ETA_REQUEST,
        at=_at(minutes),
        first_attempt=True,
    )


def _timer(
    timer_id: str,
    *,
    due: float,
    status: TimerStatus = TimerStatus.PENDING,
    load_id: str = "481207",
    obligation_id: str = "obl_1",
) -> Timer:
    return Timer(
        timer_id=timer_id,
        load_id=load_id,
        obligation_id=obligation_id,
        due_at=_at(due),
        created_at=T0,
        status=status,
    )


async def _seed(
    store: MemoryStore,
    *,
    loads: Sequence[Load] = (),
    events: Sequence[Event] = (),
    obligations: Sequence[Obligation] = (),
    timers: Sequence[Timer] = (),
    runs: Sequence[RunRecord] = (),
    trackers: Sequence[tuple[str, RiskTracker]] = (),
) -> None:
    async with store.unit_of_work() as uow:
        for load in loads:
            await uow.loads.save(load)
        for event in events:
            assert await uow.events.append(event)
        for obligation in obligations:
            await uow.obligations.save(obligation)
        for timer in timers:
            await uow.timers.save(timer)
        for run in runs:
            await uow.runs.save(run)
        for load_id, tracker in trackers:
            await uow.loads.save_tracker(load_id, tracker)
        await uow.commit()


async def _context(
    store: MemoryStore,
    event: Event,
    *,
    now: datetime = NOW,
    recent_limit: int = 30,
    window: int = WINDOW,
) -> RunContext:
    async with store.unit_of_work() as uow:
        return await load_run_context(
            _port(uow), event, now=now, recent_limit=recent_limit, outbound_window_minutes=window
        )


def _port(uow: MemoryUnitOfWork) -> UnitOfWork:
    """Each memory repository satisfies its port, but the protocol's attributes are
    mutable, so pyright treats them as invariant and rejects the concrete classes."""
    return cast(UnitOfWork, uow)


def _other_load(load: Load) -> Load:
    return load.model_copy(update={"load_id": OTHER})


# --- load, event, now, tracker -------------------------------------------------------------


@pytest.mark.unit
async def test_carries_the_event_the_load_and_the_time(store: MemoryStore, load: Load) -> None:
    event = _message("m1")
    await _seed(store, loads=[load], events=[event])

    ctx = await _context(store, event)

    assert ctx.event == event
    assert ctx.load == load
    assert ctx.now == NOW


@pytest.mark.unit
async def test_unknown_load_raises_a_clear_domain_error(store: MemoryStore) -> None:
    event = _message("m1", load_id="nope")

    with pytest.raises(DomainError, match=r"unknown load 'nope'.*'m1'"):
        await _context(store, event)


@pytest.mark.unit
async def test_reads_the_risk_tracker_of_the_load(store: MemoryStore, load: Load) -> None:
    event = _message("m1")
    tracker = RiskTracker(band=DelayRisk.HIGH)
    await _seed(
        store,
        loads=[load, _other_load(load)],
        events=[event],
        trackers=[(load.load_id, tracker), (OTHER, RiskTracker(band=DelayRisk.LOW))],
    )

    ctx = await _context(store, event)

    assert ctx.tracker == tracker


@pytest.mark.unit
async def test_tracker_is_none_when_the_load_has_none(store: MemoryStore, load: Load) -> None:
    event = _message("m1")
    await _seed(store, loads=[load], events=[event])

    ctx = await _context(store, event)

    assert ctx.tracker is None


# --- obligations and timers ----------------------------------------------------------------


@pytest.mark.unit
async def test_obligations_are_every_obligation_of_the_load_oldest_first(
    store: MemoryStore, load: Load
) -> None:
    event = _message("m1")
    closed = _obligation("obl_old", minutes=0).close(ObligationStatus.SATISFIED)
    open_ = _obligation("obl_new", minutes=30)
    foreign = _obligation("obl_foreign", minutes=10, load_id=OTHER)
    await _seed(
        store,
        loads=[load, _other_load(load)],
        events=[event],
        obligations=[open_, foreign, closed],  # saved out of order on purpose
    )

    ctx = await _context(store, event)

    assert [o.obligation_id for o in ctx.obligations] == ["obl_old", "obl_new"]
    assert ctx.open_obligation(ObligationKind.ETA_REQUEST) == open_
    assert ctx.obligation("obl_old") == closed
    assert ctx.obligation("obl_foreign") is None


@pytest.mark.unit
async def test_pending_timers_are_the_loads_pending_ones_earliest_due_first(
    store: MemoryStore, load: Load
) -> None:
    event = _message("m1")
    timers = [
        _timer("timer_late", due=120),
        _timer("timer_early", due=31),
        _timer("timer_fired", due=10, status=TimerStatus.FIRED),
        _timer("timer_superseded", due=20, status=TimerStatus.SUPERSEDED),
        _timer("timer_cancelled", due=25, status=TimerStatus.CANCELLED),
        _timer("timer_foreign", due=5, load_id=OTHER),
    ]
    await _seed(store, loads=[load, _other_load(load)], events=[event], timers=timers)

    ctx = await _context(store, event)

    assert [t.timer_id for t in ctx.pending_timers] == ["timer_early", "timer_late"]


@pytest.mark.unit
async def test_a_timer_due_event_carries_the_timer_it_names(store: MemoryStore, load: Load) -> None:
    event = _timer_due("timer_1")
    timer = _timer("timer_1", due=31, status=TimerStatus.FIRED)
    await _seed(store, loads=[load], events=[event], timers=[timer, _timer("timer_2", due=61)])

    ctx = await _context(store, event)

    assert ctx.event_timer == timer


@pytest.mark.unit
async def test_event_timer_is_none_when_the_named_timer_does_not_exist(
    store: MemoryStore, load: Load
) -> None:
    event = _timer_due("timer_gone")
    await _seed(store, loads=[load], events=[event])

    ctx = await _context(store, event)

    assert ctx.event_timer is None


@pytest.mark.unit
@pytest.mark.parametrize("data", [{}, {"timer_id": None}, {"timer_id": 7}])
async def test_event_timer_is_none_when_the_event_names_no_timer(
    store: MemoryStore, load: Load, data: dict[str, Any]
) -> None:
    event = _timer_due("timer_1").model_copy(update={"data": data})
    await _seed(store, loads=[load], events=[event], timers=[_timer("timer_1", due=31)])

    ctx = await _context(store, event)

    assert ctx.event_timer is None


@pytest.mark.unit
async def test_other_events_have_no_event_timer(store: MemoryStore, load: Load) -> None:
    event = _message("m1").model_copy(update={"data": {"timer_id": "timer_1"}})
    await _seed(store, loads=[load], events=[event], timers=[_timer("timer_1", due=31)])

    ctx = await _context(store, event)

    assert ctx.event_timer is None


# --- recent events -------------------------------------------------------------------------


@pytest.mark.unit
async def test_recent_events_are_the_last_n_of_the_load_with_the_agents_own_actions(
    store: MemoryStore, load: Load
) -> None:
    events = [
        _message("m1", minutes=0),
        _sms("e0:0", at=_at(1)),
        _message("foreign", minutes=2, load_id=OTHER),
        _message("m2", minutes=12, body="What's the PO number?"),
        _sms("m2:0", at=_at(13)),
        _message("m3", minutes=20),
    ]
    await _seed(store, loads=[load, _other_load(load)], events=events)

    ctx = await _context(store, events[-1], recent_limit=4)

    assert [e.event_id for e in ctx.recent_events] == ["act:e0:0", "m2", "act:m2:0", "m3"]


# --- recent outbound -----------------------------------------------------------------------


@pytest.mark.unit
async def test_recent_outbound_has_one_record_per_recipient_of_sms_email_and_slack(
    store: MemoryStore, load: Load
) -> None:
    email = {
        "to_contact_ids": ["dsp_4410", "brk_0087"],
        "subject": "Load 481207",
        "body": "Please send the ETA.",
        "purpose": "escalation",
    }
    events = [
        _sms("a:0", at=_at(40)),
        _action("b:0", "email", email, at=_at(41), channel=Channel.EMAIL),
        _action("c:0", "broker_slack", {"broker_id": "northline", "text": "No ETA"}, at=_at(42)),
        _action("d:0", "tms_note", {"text": "note"}, at=_at(43), channel=Channel.SYSTEM),
        _action(
            "d:1",
            "hero_task",
            {"title": "Call", "description": "", "urgent": True},
            at=_at(43),
            channel=Channel.HERO,
        ),
        _action("d:2", "internal_slack", {"text": "fyi"}, at=_at(43), channel=Channel.HERO),
        _action("d:3", "stage_update", {"stage": "at_pickup"}, at=_at(43), channel=Channel.SYSTEM),
        _message("m1", minutes=44),  # inbound SMS: not ours
        _message("m2", minutes=45),
    ]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1])

    assert ctx.recent_outbound == (
        OutboundRecord(contact_id="drv_20931", channel=Channel.SMS, at=_at(40)),
        OutboundRecord(contact_id="dsp_4410", channel=Channel.EMAIL, at=_at(41)),
        OutboundRecord(contact_id="brk_0087", channel=Channel.EMAIL, at=_at(41)),
        OutboundRecord(contact_id="brk_0087", channel=Channel.SLACK, at=_at(42)),
    )


@pytest.mark.unit
async def test_outbound_window_includes_its_start_and_excludes_anything_older(
    store: MemoryStore, load: Load
) -> None:
    start = NOW - timedelta(minutes=WINDOW)
    events = [
        _sms("old:0", at=start - timedelta(seconds=1)),
        _sms("edge:0", at=start),
        _sms("new:0", at=NOW),
        _message("m1", minutes=90),
    ]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1])

    assert [r.at for r in ctx.recent_outbound] == [start, NOW]


@pytest.mark.unit
async def test_outbound_covers_the_whole_window_even_past_the_recent_limit(
    store: MemoryStore, load: Load
) -> None:
    """A burst of chatter must not hide the SMS the rate limit has to count."""
    events = [
        _sms("old:0", at=_at(10)),
        *(_sms(f"s{i}:0", at=_at(40 + i)) for i in range(5)),
        *(_message(f"m{i}", minutes=50 + i) for i in range(6)),
    ]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1], recent_limit=3)

    assert len(ctx.recent_events) == 3
    assert [r.at for r in ctx.recent_outbound] == [_at(40 + i) for i in range(5)]


@pytest.mark.unit
async def test_outbound_is_complete_when_no_recent_events_are_requested(
    store: MemoryStore, load: Load
) -> None:
    events = [_sms("s1:0", at=_at(40)), _sms("s2:0", at=_at(41)), _message("m1", minutes=50)]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1], recent_limit=0)

    assert ctx.recent_events == ()
    assert [r.at for r in ctx.recent_outbound] == [_at(40), _at(41)]


@pytest.mark.unit
async def test_outbound_belongs_to_the_load_only(store: MemoryStore, load: Load) -> None:
    events = [
        _sms("mine:0", at=_at(40)),
        _sms("theirs:0", at=_at(41), load_id=OTHER, contact_id="drv_other"),
        _message("m1", minutes=50),
    ]
    await _seed(store, loads=[load, _other_load(load)], events=events)

    ctx = await _context(store, events[-1])

    assert [r.contact_id for r in ctx.recent_outbound] == ["drv_20931"]


@pytest.mark.unit
async def test_broker_slack_counts_against_the_first_broker_contact_on_slack(
    store: MemoryStore, load: Load
) -> None:
    contacts = (
        *load.contacts,
        Contact(id="brk_email_only", role=Role.BROKER, channels=(Channel.EMAIL,)),
        Contact(id="brk_second", role=Role.BROKER, channels=(Channel.SLACK,)),
    )
    reordered = load.model_copy(update={"contacts": (contacts[-2], *contacts[:-2], contacts[-1])})
    events = [
        _action("c:0", "broker_slack", {"broker_id": "northline", "text": "x"}, at=_at(40)),
        _message("m1", minutes=50),
    ]
    await _seed(store, loads=[reordered], events=events)

    ctx = await _context(store, events[-1])

    assert ctx.recent_outbound == (
        OutboundRecord(contact_id="brk_0087", channel=Channel.SLACK, at=_at(40)),
    )


@pytest.mark.unit
async def test_broker_slack_is_skipped_when_no_broker_contact_is_on_slack(
    store: MemoryStore, load: Load
) -> None:
    no_slack = load.model_copy(
        update={
            "contacts": tuple(
                c.model_copy(update={"channels": (Channel.EMAIL,)}) for c in load.contacts
            )
        }
    )
    events = [
        _action("c:0", "broker_slack", {"broker_id": "northline", "text": "x"}, at=_at(40)),
        _message("m1", minutes=50),
    ]
    await _seed(store, loads=[no_slack], events=events)

    ctx = await _context(store, events[-1])

    assert ctx.recent_outbound == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("sms", {"text": "no recipient"}),
        ("sms", {"contact_id": 42}),
        ("email", {"to_contact_ids": "dsp_4410"}),
        ("email", {"body": "no recipients"}),
        ("email", {"to_contact_ids": [None, 3]}),
    ],
)
async def test_malformed_outbound_payloads_produce_no_record(
    store: MemoryStore, load: Load, kind: str, payload: dict[str, Any]
) -> None:
    events = [_action("x:0", kind, payload, at=_at(40)), _message("m1", minutes=50)]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1])

    assert ctx.recent_outbound == ()


@pytest.mark.unit
@pytest.mark.parametrize("data", [{"kind": "sms"}, {"kind": "sms", "payload": "drv_20931"}])
async def test_an_agent_action_without_a_payload_mapping_produces_no_record(
    store: MemoryStore, load: Load, data: dict[str, Any]
) -> None:
    bare = _sms("x:0", at=_at(40)).model_copy(update={"data": data})
    events = [bare, _message("m1", minutes=50)]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1])

    assert ctx.recent_outbound == ()


@pytest.mark.unit
async def test_email_recipients_keep_valid_ids_and_drop_the_rest(
    store: MemoryStore, load: Load
) -> None:
    payload = {"to_contact_ids": ("dsp_4410", None), "body": "x", "purpose": "info"}
    events = [_action("x:0", "email", payload, at=_at(40)), _message("m1", minutes=50)]
    await _seed(store, loads=[load], events=events)

    ctx = await _context(store, events[-1])

    assert [r.contact_id for r in ctx.recent_outbound] == ["dsp_4410"]


# --- already processed ---------------------------------------------------------------------


@pytest.mark.unit
async def test_already_processed_when_a_run_exists_for_the_event(
    store: MemoryStore, load: Load
) -> None:
    event = _message("m1")
    run = RunRecord(
        run_id="run_1",
        event_id="m1",
        load_id=load.load_id,
        started_at=_at(1),
        status=RunStatus.NOOP,
    )
    await _seed(store, loads=[load], events=[event, _message("m2")], runs=[run])

    assert (await _context(store, event)).already_processed is True
    assert (await _context(store, _message("m2"))).already_processed is False
