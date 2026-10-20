"""Contract suite for `UnitOfWork` and its repositories (`ports/repositories.py`).

The memory store (unit tests, evals, the simulation) must pass it, each test on a fresh,
empty store.

It pins down what the port docstrings leave implicit: the order of every list and its
tie-break, a rejected duplicate that leaves the unit of work usable, queue state that
outlives the unit of work that changed it, and a transaction that commits or rolls back
as a whole. Rows are referentially whole (an event's load exists, so do a timer's
obligation, an outbox item's run and a run's event), so an implementation may enforce
foreign keys.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from freight_agent.domain.events import Attachment, Direction, Event, EventType
from freight_agent.domain.load import Channel, DelayRisk, Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.outbox import OutboxItem, OutboxKind, OutboxStatus
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.ports.repositories import UnitOfWork, UnitOfWorkFactory
from tests.contract.uow_factories import MAX_QUEUE_ATTEMPTS

pytestmark = pytest.mark.contract

LA = ZoneInfo("America/Los_Angeles")
A = "481207"  # the fixture load
B = "550031"  # a second load: the fixture under another id
DRIVER = "drv_20931"
ETA = ObligationKind.ETA_REQUEST
LEASE_S = 300.0  # five minutes, so the claim lease tests read in whole minutes
OBLIGATION_OF = {A: "obl_1", B: "obl_2"}  # the parent obligation of each load's timers
RUN_OF = {A: "run_1", B: "run_2"}  # the parent run of each load's outbox items


@pytest.fixture
async def loads(uow_factory: UnitOfWorkFactory, load: Load) -> tuple[Load, Load]:
    """Both loads, committed: every other row in the suite belongs to one of them."""
    other = load.model_copy(update={"load_id": B})
    async with uow_factory() as uow:
        await uow.loads.save(load)
        await uow.loads.save(other)
        await uow.commit()
    return load, other


def at(minute: int, second: int = 0) -> datetime:
    """07:00 plus `minute` on the morning of the main scenario."""
    return datetime(2026, 10, 13, 7, 0, tzinfo=LA) + timedelta(minutes=minute, seconds=second)


def _message(
    event_id: str, *, load_id: str = A, minute: int = 0, second: int = 0, body: str = "ok"
) -> Event:
    return Event(
        event_id=event_id,
        load_id=load_id,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=at(minute, second),
        channel=Channel.SMS,
        sender_contact_id=DRIVER,
        body=body,
    )


def _obligation(
    obligation_id: str,
    *,
    load_id: str = A,
    minute: int = 0,
    status: ObligationStatus = ObligationStatus.OPEN,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        load_id=load_id,
        kind=ETA,
        status=status,
        attempts=1,
        opened_at=at(minute),
        last_action_at=at(minute),
    )


def _timer(
    timer_id: str,
    *,
    due: int,
    load_id: str = A,
    created: int = 0,
    status: TimerStatus = TimerStatus.PENDING,
) -> Timer:
    return Timer(
        timer_id=timer_id,
        load_id=load_id,
        obligation_id=OBLIGATION_OF[load_id],
        due_at=at(due),
        created_at=at(created),
        status=status,
        reason="eta follow-up",
    )


def _run(
    run_id: str,
    event_id: str,
    *,
    load_id: str = A,
    minute: int = 0,
    status: RunStatus = RunStatus.ACTED,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        event_id=event_id,
        load_id=load_id,
        started_at=at(minute),
        finished_at=at(minute, 30),
        status=status,
        sop_section_ids=("delay_risk_high",),
    )


def _item(
    outbox_id: str,
    key: str,
    *,
    load_id: str = A,
    minute: int = 0,
    status: OutboxStatus = OutboxStatus.PENDING,
) -> OutboxItem:
    return OutboxItem(
        outbox_id=outbox_id,
        idempotency_key=key,
        run_id=RUN_OF[load_id],
        load_id=load_id,
        kind=OutboxKind.SMS,
        payload={"contact_id": DRIVER, "text": f"text of {outbox_id}", "purpose": "eta_request"},
        created_at=at(minute),
        status=status,
    )


async def _save_obligations(uow: UnitOfWork) -> None:
    for load_id, obligation_id in OBLIGATION_OF.items():
        await uow.obligations.save(_obligation(obligation_id, load_id=load_id))


async def _save_runs(uow: UnitOfWork) -> None:
    for load_id, run_id in RUN_OF.items():
        event = _message(f"m_{run_id}", load_id=load_id)
        await uow.events.append(event)
        await uow.runs.save(_run(run_id, event.event_id, load_id=load_id))


def _event_ids(events: Sequence[Event]) -> list[str]:
    return [e.event_id for e in events]


def _timer_ids(timers: Sequence[Timer]) -> list[str]:
    return [t.timer_id for t in timers]


def _outbox_ids(items: Sequence[OutboxItem]) -> list[str]:
    return [i.outbox_id for i in items]


# --- Loads -------------------------------------------------------------------------


async def test_a_saved_load_is_read_back_and_listed_by_id(
    uow_factory: UnitOfWorkFactory, load: Load
) -> None:
    other = load.model_copy(update={"load_id": B})
    async with uow_factory() as uow:
        assert await uow.loads.get(A) is None
        assert await uow.loads.list_ids() == []
        await uow.loads.save(other)
        await uow.loads.save(load)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.loads.get(A) == load
        assert await uow.loads.get(B) == other
        assert await uow.loads.list_ids() == [A, B]


async def test_saving_a_load_again_replaces_it(uow_factory: UnitOfWorkFactory, load: Load) -> None:
    moved = load.model_copy(update={"stage": Stage.AT_PICKUP, "delay_risk": DelayRisk.HIGH})
    async with uow_factory() as uow:
        await uow.loads.save(load)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.loads.save(moved)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.loads.get(A) == moved
        assert await uow.loads.list_ids() == [A]


@pytest.mark.usefixtures("loads")
async def test_a_risk_tracker_is_saved_and_replaced_per_load(
    uow_factory: UnitOfWorkFactory,
) -> None:
    upgrading = RiskTracker(band=DelayRisk.LOW, pending=DelayRisk.HIGH, pending_count=1)
    async with uow_factory() as uow:
        assert await uow.loads.get_tracker(A) is None
        await uow.loads.save_tracker(A, RiskTracker(band=DelayRisk.LOW))
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.loads.get_tracker(A) == RiskTracker(band=DelayRisk.LOW)
        await uow.loads.save_tracker(A, upgrading)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.loads.get_tracker(A) == upgrading
        assert await uow.loads.get_tracker(B) is None


@pytest.mark.usefixtures("loads")
async def test_a_risk_tracker_keeps_its_recent_ping_ids_in_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The ping dedupe lives with the tracker, so it outlives the process that wrote it."""
    seen = RiskTracker(band=DelayRisk.HIGH, recent_ping_ids=("p1", "p10", "p2"))
    async with uow_factory() as uow:
        await uow.loads.save_tracker(A, seen)
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.loads.get_tracker(A) == seen
        await uow.loads.save_tracker(A, seen.remember("p3", keep=2))
        await uow.commit()

    async with uow_factory() as uow:
        tracker = await uow.loads.get_tracker(A)
    assert tracker is not None and tracker.recent_ping_ids == ("p2", "p3")


# --- Event log ---------------------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_a_duplicate_append_returns_false_and_stores_nothing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first = _message("m1", body="first")
    async with uow_factory() as uow:
        assert await uow.events.append(first) is True
        assert await uow.events.append(_message("m1", minute=5, body="second")) is False
        # The rejected duplicate leaves the unit of work usable.
        assert await uow.events.append(_message("m2", minute=1)) is True
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.events.append(_message("m1", minute=9, body="third")) is False
        assert await uow.events.get("m1") == first
        assert _event_ids(await uow.events.recent(A, 10)) == ["m1", "m2"]


@pytest.mark.usefixtures("loads")
async def test_recent_returns_the_newest_events_of_the_load_oldest_first(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        for minute, event_id in enumerate(["a1", "b1", "a2", "a3", "b2", "a4"]):
            load_id = A if event_id.startswith("a") else B
            await uow.events.append(_message(event_id, load_id=load_id, minute=minute))
        await uow.commit()

    async with uow_factory() as uow:
        assert _event_ids(await uow.events.recent(A, 3)) == ["a2", "a3", "a4"]
        assert _event_ids(await uow.events.recent(A, 10)) == ["a1", "a2", "a3", "a4"]
        assert _event_ids(await uow.events.recent(B, 1)) == ["b2"]
        assert await uow.events.recent(A, 0) == []
        assert await uow.events.recent("no_such_load", 10) == []


@pytest.mark.usefixtures("loads")
async def test_recent_keeps_the_order_of_the_log_not_of_occurred_at(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A message carries the time it was sent, and an SMS can arrive after a later event.

    The run context cuts the history at the event being processed by its position in
    this list (docs/decisions.md, Run context). Sorted by `occurred_at`, the late reply
    would come before the agent's SMS, and its run would not see that SMS as history.
    """
    asked = Event(
        event_id="act:risk:p1:0",
        load_id=A,
        type=EventType.AGENT_ACTION,
        occurred_at=at(1),
        channel=Channel.SMS,
        direction=Direction.OUTBOUND,
        body="What time will you arrive at the pickup?",
    )
    late_reply = _message("m1", second=50, body="running late, around 9")
    async with uow_factory() as uow:
        await uow.events.append(asked)
        await uow.events.append(late_reply)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.events.recent(A, 10) == [asked, late_reply]
        assert await uow.events.recent(A, 1) == [late_reply]


@pytest.mark.usefixtures("loads")
async def test_get_returns_the_logged_event_whole(uow_factory: UnitOfWorkFactory) -> None:
    document = _message("m1", body="BOL attached").model_copy(
        update={
            "attachments": (
                Attachment(
                    attachment_id="att_1",
                    filename="bol.pdf",
                    content_type="application/pdf",
                    ref="blob://att_1",
                ),
            )
        }
    )
    action = Event(
        event_id="act:m1:0",
        load_id=A,
        type=EventType.AGENT_ACTION,
        occurred_at=at(1),
        channel=Channel.HERO,
        direction=Direction.OUTBOUND,
        body="Review the BOL",
        data={
            "kind": "hero_task",
            "payload": {"title": "Review the BOL", "description": "", "urgent": False},
            "run_id": "run_1",
            "outbox_id": "out_1",
        },
    )
    async with uow_factory() as uow:
        await uow.events.append(document)
        await uow.events.append(action)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.events.get("m1") == document
        assert await uow.events.get("act:m1:0") == action
        assert await uow.events.get("no_such_event") is None


# --- Obligations -------------------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_get_open_returns_only_the_open_obligation_of_the_kind(
    uow_factory: UnitOfWorkFactory,
) -> None:
    satisfied = _obligation("obl_1", status=ObligationStatus.SATISFIED)
    current = _obligation("obl_2", minute=30)
    async with uow_factory() as uow:
        await uow.obligations.save(satisfied)
        await uow.obligations.save(current)
        await uow.obligations.save(
            _obligation("obl_3", load_id=B, status=ObligationStatus.CANCELLED)
        )
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.obligations.get_open(A, ETA) == current
        assert await uow.obligations.get_open(B, ETA) is None
        assert await uow.obligations.get("obl_1") == satisfied
        assert await uow.obligations.get("no_such_obligation") is None


@pytest.mark.usefixtures("loads")
async def test_list_for_load_orders_obligations_by_opened_at_then_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        for obligation in (
            _obligation("obl_c", minute=30),
            _obligation("obl_b", status=ObligationStatus.EXHAUSTED),
            _obligation("obl_x", load_id=B),
            _obligation("obl_a", minute=30, status=ObligationStatus.SATISFIED),
        ):
            await uow.obligations.save(obligation)
        await uow.commit()

    async with uow_factory() as uow:
        listed = await uow.obligations.list_for_load(A)
        assert [o.obligation_id for o in listed] == ["obl_b", "obl_a", "obl_c"]


@pytest.mark.usefixtures("loads")
async def test_saving_an_obligation_again_updates_it(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        await uow.obligations.save(_obligation("obl_1"))
        await uow.commit()
    async with uow_factory() as uow:
        obligation = await uow.obligations.get("obl_1")
        assert obligation is not None
        await uow.obligations.save(obligation.record_attempt(at(30)).attach_timer("timer_2"))
        await uow.commit()
    async with uow_factory() as uow:
        updated = await uow.obligations.get("obl_1")
        assert updated is not None
        assert (updated.attempts, updated.last_action_at, updated.active_timer_id) == (
            2,
            at(30),
            "timer_2",
        )
        await uow.obligations.save(updated.close(ObligationStatus.SATISFIED))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.obligations.get_open(A, ETA) is None
        listed = await uow.obligations.list_for_load(A)
        assert [o.status for o in listed] == [ObligationStatus.SATISFIED]


# --- Timers ------------------------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_due_returns_pending_timers_up_to_now_earliest_first_ties_by_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await _save_obligations(uow)
        for timer in (
            _timer("timer_b", due=30),
            _timer("timer_future", due=31),
            _timer("timer_a", due=30, load_id=B),
            _timer("timer_early", due=10),
            _timer("timer_fired", due=0, status=TimerStatus.FIRED),
            _timer("timer_superseded", due=5, status=TimerStatus.SUPERSEDED),
            _timer("timer_cancelled", due=5, status=TimerStatus.CANCELLED),
        ):
            await uow.timers.save(timer)
        await uow.commit()

    async with uow_factory() as uow:
        # Instants, not wall times: 14:30 UTC is 07:30 in Los Angeles.
        due = await uow.timers.due(at(30).astimezone(UTC))
        assert _timer_ids(due) == ["timer_early", "timer_a", "timer_b"]
        assert await uow.timers.due(at(9)) == []


@pytest.mark.usefixtures("loads")
async def test_next_due_at_is_the_earliest_pending_due_time(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        assert await uow.timers.next_due_at() is None
        await _save_obligations(uow)
        await uow.timers.save(_timer("timer_fired", due=0, status=TimerStatus.FIRED))
        await uow.timers.save(_timer("timer_cancelled", due=5, status=TimerStatus.CANCELLED))
        assert await uow.timers.next_due_at() is None
        await uow.timers.save(_timer("timer_late", due=40))
        await uow.timers.save(_timer("timer_soon", due=20, load_id=B))
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.timers.next_due_at() == at(20)
        soon = await uow.timers.get("timer_soon")
        assert soon is not None
        await uow.timers.save(soon.mark(TimerStatus.FIRED))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.timers.next_due_at() == at(40)
        assert await uow.timers.get("timer_soon") == soon.mark(TimerStatus.FIRED)
        assert _timer_ids(await uow.timers.list_for_load(B)) == ["timer_soon"]
        assert await uow.timers.get("no_such_timer") is None


@pytest.mark.usefixtures("loads")
async def test_pending_for_obligation_returns_its_pending_timers_earliest_first(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await _save_obligations(uow)
        for timer in (
            _timer("timer_3", due=40),
            _timer("timer_1", due=30, status=TimerStatus.SUPERSEDED),
            _timer("timer_2", due=20),
            _timer("timer_0", due=40),
            _timer("timer_4", due=10, load_id=B),
        ):
            await uow.timers.save(timer)
        await uow.commit()

    async with uow_factory() as uow:
        pending = await uow.timers.pending_for_obligation(OBLIGATION_OF[A])
        assert _timer_ids(pending) == ["timer_2", "timer_0", "timer_3"]
        pending = await uow.timers.pending_for_obligation(OBLIGATION_OF[B])
        assert _timer_ids(pending) == ["timer_4"]
        assert await uow.timers.pending_for_obligation("no_such_obligation") == []


@pytest.mark.usefixtures("loads")
async def test_list_for_load_orders_timers_by_created_at_then_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await _save_obligations(uow)
        for timer in (
            _timer("timer_c", due=60, created=1, status=TimerStatus.SUPERSEDED),
            _timer("timer_a", due=30, created=0, status=TimerStatus.FIRED),
            _timer("timer_x", due=30, load_id=B),
            _timer("timer_b", due=90, created=1),
        ):
            await uow.timers.save(timer)
        await uow.commit()

    async with uow_factory() as uow:
        listed = await uow.timers.list_for_load(A)
        assert _timer_ids(listed) == ["timer_a", "timer_b", "timer_c"]


# --- Outbox ------------------------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_a_duplicate_idempotency_key_returns_false_and_stores_nothing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first = _item("out_1", "m1:0")
    async with uow_factory() as uow:
        await _save_runs(uow)
        assert await uow.outbox.add(first) is True
        assert await uow.outbox.add(_item("out_2", "m1:0", minute=1)) is False
        # The rejected duplicate leaves the unit of work usable.
        assert await uow.outbox.add(_item("out_3", "m1:1")) is True
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.outbox.add(_item("out_4", "m1:0", minute=2)) is False
        listed = await uow.outbox.list_for_load(A)
        assert _outbox_ids(listed) == ["out_1", "out_3"]
        assert listed[0] == first
        assert _outbox_ids(await uow.outbox.pending()) == ["out_1", "out_3"]


@pytest.mark.usefixtures("loads")
async def test_pending_returns_pending_items_oldest_first_up_to_the_limit(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await _save_runs(uow)
        for item in (
            _item("out_c", "m1:2", minute=1),
            _item("out_sent", "m0:0", status=OutboxStatus.SENT),
            _item("out_a", "m2:0", load_id=B, minute=1),
            _item("out_failed", "m0:1", status=OutboxStatus.FAILED),
            _item("out_b", "m1:0"),
        ):
            await uow.outbox.add(item)
        await uow.commit()

    async with uow_factory() as uow:
        assert _outbox_ids(await uow.outbox.pending()) == ["out_b", "out_a", "out_c"]
        assert _outbox_ids(await uow.outbox.pending(limit=2)) == ["out_b", "out_a"]
        assert await uow.outbox.pending(limit=0) == []


@pytest.mark.usefixtures("loads")
async def test_saving_an_outbox_item_updates_its_status(uow_factory: UnitOfWorkFactory) -> None:
    item = _item("out_1", "m1:0")
    sent = item.model_copy(update={"status": OutboxStatus.SENT, "attempts": 1})
    async with uow_factory() as uow:
        await _save_runs(uow)
        await uow.outbox.add(item)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.outbox.save(sent)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.outbox.pending() == []
        assert await uow.outbox.list_for_load(A) == [sent]
        # Saving keeps the idempotency key taken.
        assert await uow.outbox.add(_item("out_2", "m1:0")) is False


@pytest.mark.usefixtures("loads")
async def test_list_for_load_orders_outbox_items_by_created_at_then_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await _save_runs(uow)
        for item in (
            _item("out_c", "m1:2", minute=1, status=OutboxStatus.SENT),
            _item("out_a", "m1:1", minute=1),
            _item("out_x", "m2:0", load_id=B),
            _item("out_b", "m1:0", status=OutboxStatus.FAILED),
        ):
            await uow.outbox.add(item)
        await uow.commit()

    async with uow_factory() as uow:
        listed = await uow.outbox.list_for_load(A)
        assert _outbox_ids(listed) == ["out_b", "out_a", "out_c"]


# --- Runs --------------------------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_for_event_returns_the_latest_run_of_the_event(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A retry runs the event again. Runs that start at the same time are ordered by id,
    as in `list_for_load`, so `for_event` is the last of the event's runs there."""
    first_try = _run("run_1", "m1", status=RunStatus.FAILED)
    async with uow_factory() as uow:
        for event_id in ("m1", "m2", "m3"):
            await uow.events.append(_message(event_id))
        for run in (
            _run("run_2", "m1", minute=5),
            first_try,
            _run("run_3", "m2", minute=10),
            _run("run_4", "m3", minute=20, status=RunStatus.FAILED),
            _run("run_5", "m3", minute=20),
        ):
            await uow.runs.save(run)
        await uow.commit()

    async with uow_factory() as uow:
        for event_id, latest in (("m1", "run_2"), ("m2", "run_3"), ("m3", "run_5")):
            run = await uow.runs.for_event(event_id)
            assert run is not None and run.run_id == latest
        assert await uow.runs.for_event("no_such_event") is None
        assert await uow.runs.get("run_1") == first_try
        assert await uow.runs.get("no_such_run") is None


@pytest.mark.usefixtures("loads")
async def test_list_for_load_orders_runs_by_started_at_then_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await uow.events.append(_message("m1"))
        await uow.events.append(_message("m2", load_id=B))
        for run in (
            _run("run_c", "m1", minute=5),
            _run("run_a", "m1", status=RunStatus.FAILED),
            _run("run_x", "m2", load_id=B),
            _run("run_b", "m1", minute=5),
        ):
            await uow.runs.save(run)
        await uow.commit()
    retried = _run("run_a", "m1", status=RunStatus.NOOP)
    async with uow_factory() as uow:
        await uow.runs.save(retried)
        await uow.commit()

    async with uow_factory() as uow:
        listed = await uow.runs.list_for_load(A)
        assert [r.run_id for r in listed] == ["run_a", "run_b", "run_c"]
        assert listed[0] == retried


# --- Queue -------------------------------------------------------------------------
# Each step runs in a unit of work of its own, as the runtime's do: a claim, a
# completion and a release must outlive the unit of work that made them.


async def _ingest(uow_factory: UnitOfWorkFactory, *events: Event) -> None:
    """Log and enqueue each event, as the runtime ingests one."""
    async with uow_factory() as uow:
        for event in events:
            assert await uow.events.append(event)
            await uow.queue.enqueue(event)
        await uow.commit()


async def _claim(uow_factory: UnitOfWorkFactory, now: datetime | None = None) -> str | None:
    """Claim at `now` (07:00 by default): the claim time the lease counts from."""
    async with uow_factory() as uow:
        event = await uow.queue.claim_next(now or at(0))
        await uow.commit()
    return None if event is None else event.event_id


async def _complete(uow_factory: UnitOfWorkFactory, event_id: str) -> None:
    async with uow_factory() as uow:
        await uow.queue.complete(event_id)
        await uow.commit()


async def _release(uow_factory: UnitOfWorkFactory, event_id: str) -> None:
    async with uow_factory() as uow:
        await uow.queue.release(event_id, "model timeout")
        await uow.commit()


async def _reclaim(uow_factory: UnitOfWorkFactory, now: datetime) -> int:
    async with uow_factory() as uow:
        reclaimed = await uow.queue.reclaim_expired(now, LEASE_S)
        await uow.commit()
    return reclaimed


async def _dead(uow_factory: UnitOfWorkFactory) -> list[str]:
    async with uow_factory() as uow:
        return _event_ids(await uow.queue.dead_letters())


async def _waiting(uow_factory: UnitOfWorkFactory) -> int:
    async with uow_factory() as uow:
        return await uow.queue.size()


@pytest.mark.usefixtures("loads")
async def test_the_queue_hands_out_a_loads_events_in_arrival_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    # a3 was sent before a2 but arrived after it: first in, first out.
    await _ingest(uow_factory, _message("a1"), _message("a2", minute=2), _message("a3", minute=1))

    claimed: list[str] = []
    while (event_id := await _claim(uow_factory)) is not None:
        claimed.append(event_id)
        await _complete(uow_factory, event_id)
    assert claimed == ["a1", "a2", "a3"]


@pytest.mark.usefixtures("loads")
async def test_one_event_per_load_is_in_flight_while_other_loads_proceed(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first = _message("a1")
    await _ingest(uow_factory, first, _message("a2", minute=1), _message("b1", load_id=B))

    async with uow_factory() as uow:
        assert await uow.queue.claim_next(at(0)) == first
        await uow.commit()
    # a2 waits behind a1, which is in flight on the same load; b1's load is free.
    assert await _claim(uow_factory) == "b1"
    assert await _claim(uow_factory) is None


@pytest.mark.usefixtures("loads")
async def test_complete_frees_the_load_for_its_next_event(uow_factory: UnitOfWorkFactory) -> None:
    await _ingest(uow_factory, _message("a1"), _message("a2", minute=1))
    assert await _claim(uow_factory) == "a1"
    assert await _claim(uow_factory) is None

    await _complete(uow_factory, "a1")
    assert await _claim(uow_factory) == "a2"
    await _complete(uow_factory, "a2")
    assert await _claim(uow_factory) is None
    assert await _waiting(uow_factory) == 0


@pytest.mark.usefixtures("loads")
async def test_a_released_event_goes_back_to_its_place_in_the_queue(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """In front of its load's later events, and still behind the events that arrived
    before it: claim_next hands out the oldest queued event, and a failure does not
    make an event younger."""
    await _ingest(
        uow_factory,
        _message("b1", load_id=B),
        _message("b2", load_id=B, minute=1),
        _message("a1", minute=2),
        _message("a2", minute=3),
    )
    assert await _claim(uow_factory) == "b1"
    assert await _claim(uow_factory) == "a1"  # b2 waits behind b1

    await _release(uow_factory, "a1")
    await _complete(uow_factory, "b1")
    assert await _claim(uow_factory) == "b2"
    assert await _claim(uow_factory) == "a1"
    assert await _claim(uow_factory) is None


@pytest.mark.usefixtures("loads")
async def test_after_max_attempts_a_released_event_is_a_dead_letter(
    uow_factory: UnitOfWorkFactory,
) -> None:
    failing = _message("a1")
    await _ingest(uow_factory, failing, _message("a2", minute=1))
    for _ in range(MAX_QUEUE_ATTEMPTS - 1):
        assert await _claim(uow_factory) == "a1"
        await _release(uow_factory, "a1")
        async with uow_factory() as uow:
            assert await uow.queue.dead_letters() == []

    assert await _claim(uow_factory) == "a1"
    await _release(uow_factory, "a1")
    async with uow_factory() as uow:
        assert await uow.queue.dead_letters() == [failing]
        assert await uow.queue.size() == 1
    # The dead letter frees its load and is never handed out again.
    assert await _claim(uow_factory) == "a2"
    await _complete(uow_factory, "a2")
    assert await _claim(uow_factory) is None


@pytest.mark.usefixtures("loads")
async def test_size_counts_the_events_waiting_to_be_claimed(
    uow_factory: UnitOfWorkFactory,
) -> None:
    assert await _waiting(uow_factory) == 0
    await _ingest(uow_factory, _message("a1"), _message("a2", minute=1), _message("b1", load_id=B))
    assert await _waiting(uow_factory) == 3

    assert await _claim(uow_factory) == "a1"
    assert await _waiting(uow_factory) == 2  # in flight is not waiting
    await _release(uow_factory, "a1")
    assert await _waiting(uow_factory) == 3
    assert await _claim(uow_factory) == "a1"
    await _complete(uow_factory, "a1")
    assert await _waiting(uow_factory) == 2


@pytest.mark.usefixtures("loads")
async def test_reclaim_expired_returns_claims_older_than_the_lease_to_their_place(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A worker that dies between claim and commit (a crash, a cancelled run) leaves its
    event in flight, and every later event of the load would wait forever. The claim is
    timed with the caller's clock, the `now` given to `claim_next`, and the lease compares
    it with the `now` given to `reclaim_expired`: the database's own clock plays no part."""
    await _ingest(
        uow_factory, _message("a1"), _message("a2", minute=1), _message("b1", load_id=B, minute=2)
    )
    assert await _claim(uow_factory, at(0)) == "a1"
    assert await _claim(uow_factory, at(4)) == "b1"

    assert await _reclaim(uow_factory, at(5)) == 0  # exactly the lease: not expired yet
    assert await _reclaim(uow_factory, at(5, 1)) == 1  # a1; b1 was claimed at 07:04

    assert await _waiting(uow_factory) == 2
    assert await _claim(uow_factory, at(5, 1)) == "a1"  # back ahead of a2
    assert await _claim(uow_factory, at(5, 1)) is None  # b1 is still in flight
    assert await _reclaim(uow_factory, at(9, 1)) == 1  # b1, now
    assert await _reclaim(uow_factory, at(9, 1)) == 0  # a reclaimed event is not counted twice


@pytest.mark.usefixtures("loads")
async def test_a_reclaimed_claim_counts_as_a_failed_attempt(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """So an event whose run always dies still becomes a dead letter."""
    poison = _message("a1")
    await _ingest(uow_factory, poison, _message("a2", minute=1))
    assert await _claim(uow_factory, at(0)) == "a1"
    await _release(uow_factory, "a1")  # an ordinary failure counts with the expired claims

    assert await _claim(uow_factory, at(1)) == "a1"
    assert await _reclaim(uow_factory, at(10)) == 1
    assert await _dead(uow_factory) == []
    assert await _claim(uow_factory, at(10)) == "a1"
    assert await _reclaim(uow_factory, at(20)) == 1

    async with uow_factory() as uow:
        assert await uow.queue.dead_letters() == [poison]
    assert await _claim(uow_factory, at(20)) == "a2"
    assert await _reclaim(uow_factory, at(20)) == 0


@pytest.mark.usefixtures("loads")
async def test_a_late_release_or_completion_of_a_reclaimed_event_changes_nothing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A run that outlived its lease finds its event back in the queue. Its release must
    not count a second failure or queue the event twice, and its completion must not take
    the queued event away (the event runs again; its stored run makes that a skip)."""
    await _ingest(uow_factory, _message("a1"), _message("a2", minute=1))
    assert await _claim(uow_factory, at(0)) == "a1"
    assert await _reclaim(uow_factory, at(6)) == 1

    await _release(uow_factory, "a1")
    await _complete(uow_factory, "a1")

    assert await _waiting(uow_factory) == 2
    for _ in range(MAX_QUEUE_ATTEMPTS - 2):  # the lease already counted one failure
        assert await _claim(uow_factory, at(6)) == "a1"
        await _release(uow_factory, "a1")
    assert await _dead(uow_factory) == []
    assert await _claim(uow_factory, at(6)) == "a1"
    await _release(uow_factory, "a1")
    assert await _dead(uow_factory) == ["a1"]
    assert await _claim(uow_factory, at(6)) == "a2"
    assert await _claim(uow_factory, at(6)) is None


# --- Transactions ------------------------------------------------------------------


async def _write_baseline(uow: UnitOfWork, load: Load) -> dict[str, object]:
    """A row in every repository and an event waiting in the queue.

    Returns what `_read_everything` reads back once they are committed.
    """
    other = load.model_copy(update={"load_id": B})
    tracker = RiskTracker(band=DelayRisk.LOW)
    logged, queued = _message("m0"), _message("m1", minute=1)
    obligation = _obligation("obl_1")
    timer = _timer("timer_1", due=30)
    run = _run("run_1", "m0")
    item = _item("out_1", "m0:0")
    await uow.loads.save(load)
    await uow.loads.save(other)
    await uow.loads.save_tracker(A, tracker)
    await uow.events.append(logged)
    await uow.events.append(queued)
    await uow.queue.enqueue(queued)
    await uow.obligations.save(obligation)
    await uow.timers.save(timer)
    await uow.runs.save(run)
    await uow.outbox.add(item)
    return {
        "load_ids": [A, B],
        "loads": [load, other],
        "tracker": tracker,
        "events": [logged, queued],
        "obligations": [obligation],
        "timers": [timer],
        "runs": [run],
        "outbox": [item],
        "waiting": 1,
    }


async def _commit_baseline(uow_factory: UnitOfWorkFactory, load: Load) -> dict[str, object]:
    async with uow_factory() as uow:
        baseline = await _write_baseline(uow, load)
        await uow.commit()
    return baseline


async def _read_everything(uow: UnitOfWork) -> dict[str, object]:
    return {
        "load_ids": await uow.loads.list_ids(),
        "loads": [await uow.loads.get(A), await uow.loads.get(B)],
        "tracker": await uow.loads.get_tracker(A),
        "events": await uow.events.recent(A, 100),
        "obligations": await uow.obligations.list_for_load(A),
        "timers": await uow.timers.list_for_load(A),
        "runs": await uow.runs.list_for_load(A),
        "outbox": await uow.outbox.list_for_load(A),
        "waiting": await uow.queue.size(),
    }


async def _change_everything(uow: UnitOfWork, load: Load) -> None:
    """Insert and update in every repository of the baseline, enqueue, and claim."""
    await uow.loads.save(load.model_copy(update={"stage": Stage.AT_PICKUP}))
    await uow.loads.save(load.model_copy(update={"load_id": "990001"}))
    await uow.loads.save_tracker(A, RiskTracker(band=DelayRisk.HIGH, recent_ping_ids=("p9",)))
    for arrived in (_message("m2", minute=2), _message("m3", minute=3)):
        await uow.events.append(arrived)
        await uow.queue.enqueue(arrived)
    assert await uow.queue.claim_next(at(3)) is not None

    obligation = await uow.obligations.get("obl_1")
    timer = await uow.timers.get("timer_1")
    run = await uow.runs.get("run_1")
    assert obligation is not None and timer is not None and run is not None
    [item] = await uow.outbox.list_for_load(A)
    await uow.obligations.save(obligation.record_attempt(at(30)))
    await uow.obligations.save(_obligation("obl_9", minute=30, status=ObligationStatus.SATISFIED))
    await uow.timers.save(timer.mark(TimerStatus.FIRED))
    await uow.timers.save(_timer("timer_9", due=60))
    await uow.runs.save(run.model_copy(update={"status": RunStatus.NOOP}))
    await uow.runs.save(_run("run_9", "m2"))
    await uow.outbox.save(item.model_copy(update={"status": OutboxStatus.SENT}))
    await uow.outbox.add(_item("out_9", "m2:0"))


def _unchanged(seen: dict[str, object], baseline: dict[str, object]) -> list[str]:
    return [key for key in baseline if seen[key] == baseline[key]]


async def test_committed_changes_are_visible_to_the_next_unit_of_work(
    uow_factory: UnitOfWorkFactory, load: Load
) -> None:
    async with uow_factory() as uow:
        expected = await _write_baseline(uow, load)
        await uow.commit()

    async with uow_factory() as uow:
        assert await _read_everything(uow) == expected


async def test_leaving_without_commit_discards_every_change(
    uow_factory: UnitOfWorkFactory, load: Load
) -> None:
    baseline = await _commit_baseline(uow_factory, load)
    async with uow_factory() as uow:
        await _change_everything(uow, load)
        # Every repository did change inside the unit of work, so none passes vacuously.
        assert _unchanged(await _read_everything(uow), baseline) == []

    async with uow_factory() as uow:
        assert await _read_everything(uow) == baseline
        claimed = await uow.queue.claim_next(at(4))  # the discarded claim is undone too
        assert claimed is not None and claimed.event_id == "m1"


class _Boom(Exception):
    pass


@pytest.mark.parametrize("commit_first", [False, True], ids=["uncommitted", "after-commit"])
async def test_an_exception_inside_the_block_rolls_everything_back(
    uow_factory: UnitOfWorkFactory, load: Load, commit_first: bool
) -> None:
    """`commit()` takes effect when the block ends normally: an exception raised after
    it still rolls the whole unit of work back, so a run that fails late leaves nothing."""
    baseline = await _commit_baseline(uow_factory, load)
    with pytest.raises(_Boom):
        async with uow_factory() as uow:
            await _change_everything(uow, load)
            if commit_first:
                await uow.commit()
            raise _Boom

    async with uow_factory() as uow:
        assert await _read_everything(uow) == baseline


@pytest.mark.usefixtures("loads")
async def test_concurrent_units_of_work_are_serialized(uow_factory: UnitOfWorkFactory) -> None:
    """Each worker reads the attempt count, waits, then writes it plus one.

    Interleaved units of work would all read the same count, and every increment but
    one would be lost.
    """
    workers = 5
    async with uow_factory() as uow:
        await uow.obligations.save(_obligation("obl_1"))
        await uow.commit()

    async def record_attempt(minute: int) -> None:
        async with uow_factory() as uow:
            obligation = await uow.obligations.get("obl_1")
            assert obligation is not None
            await asyncio.sleep(0.01)  # room for another unit of work to read the same count
            await uow.obligations.save(obligation.record_attempt(at(minute)))
            await uow.commit()

    await asyncio.gather(*(record_attempt(minute) for minute in range(1, workers + 1)))

    async with uow_factory() as uow:
        obligation = await uow.obligations.get("obl_1")
    assert obligation is not None and obligation.attempts == 1 + workers


async def _oldest(uow_factory: UnitOfWorkFactory) -> str | None:
    async with uow_factory() as uow:
        event = await uow.queue.oldest_queued()
    return None if event is None else event.event_id


@pytest.mark.usefixtures("loads")
async def test_oldest_queued_is_the_longest_waiting_unclaimed_event(
    uow_factory: UnitOfWorkFactory,
) -> None:
    assert await _oldest(uow_factory) is None
    await _ingest(uow_factory, _message("a1"), _message("b1", minute=1, load_id=B))
    assert await _oldest(uow_factory) == "a1"

    assert await _claim(uow_factory) == "a1"
    assert await _oldest(uow_factory) == "b1"  # in flight is not waiting
    await _release(uow_factory, "a1")
    assert await _oldest(uow_factory) == "a1"
