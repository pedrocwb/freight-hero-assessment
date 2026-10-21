from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.context import RunContext
from freight_agent.application.executor import ExecutionResult, Executor, SkippedAction
from freight_agent.domain.actions import (
    Action,
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    DeferObligation,
    OpenObligation,
    PostBrokerSlack,
    PostInternalSlack,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.broker import Broker, BrokerLimits, DocumentRequirement, standard_broker
from freight_agent.domain.documents import Document, DocumentStatus
from freight_agent.domain.events import Direction, Event, EventType
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import DeferralPolicy, Obligation, ObligationKind
from freight_agent.domain.obligations import ObligationStatus as Status
from freight_agent.domain.outbox import OutboxItem, OutboxKind, OutboxStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.domain.validation import ValidationConfig
from freight_agent.interfaces.repositories import UnitOfWork

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
LOAD_ID = "481207"
DRIVER = "drv_20931"
DISPATCHER = "dsp_4410"
ETA = ObligationKind.ETA_REQUEST
ASK_ETA = SendSms(
    contact_id=DRIVER, text="What time will you arrive at the pickup?", purpose=Purpose.ETA_REQUEST
)


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=LA)


def _risk_high(ping_id: str = "ping_1", occurred_at: datetime | None = None) -> Event:
    return Event(
        event_id=f"risk:{ping_id}",
        load_id=LOAD_ID,
        type=EventType.RISK_CHANGED,
        occurred_at=occurred_at or at(7, 0),
        channel=Channel.SYSTEM,
        data={"previous": "low", "current": "high"},
    )


def _driver_sms(event_id: str, occurred_at: datetime, body: str) -> Event:
    return Event(
        event_id=event_id,
        load_id=LOAD_ID,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=occurred_at,
        channel=Channel.SMS,
        sender_contact_id=DRIVER,
        body=body,
    )


def _timer_due(timer_id: str, occurred_at: datetime) -> Event:
    return Event(
        event_id=f"timer:{timer_id}",
        load_id=LOAD_ID,
        type=EventType.TIMER_DUE,
        occurred_at=occurred_at,
        channel=Channel.SYSTEM,
        data={"timer_id": timer_id, "obligation_id": "obl_1", "kind": ETA.value},
    )


class _World:
    """One load in a memory store, a virtual clock and the executor under test.

    Each `run` plays one agent run: the event is in the log, the context is
    built by hand from the store, and the approved actions are applied in one
    unit of work.
    """

    def __init__(self, load: Load, broker: Broker | None = None) -> None:
        self.store = MemoryStore()
        self.clock = VirtualClock(at(6, 55))
        self.ids = SequentialIdGenerator()
        self.load = load
        self.broker = broker or standard_broker()
        self.executor = Executor(self.clock, self.ids)

    async def register(self) -> None:
        async with self.store.unit_of_work() as uow:
            await uow.loads.save(self.load)
            await uow.commit()

    async def run(
        self, event: Event, *actions: Action, at_time: datetime, commit: bool = True
    ) -> ExecutionResult:
        self.clock.set(at_time)
        async with self.store.unit_of_work() as uow:
            await uow.events.append(event)
            ctx = await self._context(uow, event)
            result = await self.executor.apply(
                uow, run_id=self.ids.new("run"), ctx=ctx, actions=actions
            )
            if commit:
                await uow.commit()
        return result

    async def fire(self, timer_id: str, at_time: datetime) -> None:
        """What the runtime does when a timer is due: the timer becomes fired."""
        self.clock.set(at_time)
        async with self.store.unit_of_work() as uow:
            timer = await uow.timers.get(timer_id)
            assert timer is not None
            await uow.timers.save(timer.mark(TimerStatus.FIRED))
            await uow.commit()

    async def _context(self, uow: UnitOfWork, event: Event) -> RunContext:
        load = await uow.loads.get(LOAD_ID)
        assert load is not None
        timers = await uow.timers.list_for_load(LOAD_ID)
        pending = [t for t in timers if t.status is TimerStatus.PENDING]
        event_timer = None
        if event.type is EventType.TIMER_DUE:
            event_timer = await uow.timers.get(event.data["timer_id"])
        return RunContext(
            event=event,
            load=load,
            now=self.clock.now(),
            obligations=tuple(await uow.obligations.list_for_load(LOAD_ID)),
            pending_timers=tuple(sorted(pending, key=lambda t: t.due_at)),
            recent_events=(event,),
            recent_outbound=(),
            event_timer=event_timer,
            broker=self.broker,
        )

    async def save_document(self, document: Document) -> None:
        async with self.store.unit_of_work() as uow:
            await uow.documents.save(document)
            await uow.commit()

    async def obligations(self) -> list[Obligation]:
        async with self.store.unit_of_work() as uow:
            return await uow.obligations.list_for_load(LOAD_ID)

    async def obligation(self) -> Obligation:
        (only,) = await self.obligations()
        return only

    async def timers(self) -> dict[str, Timer]:
        async with self.store.unit_of_work() as uow:
            return {t.timer_id: t for t in await uow.timers.list_for_load(LOAD_ID)}

    async def outbox(self) -> list[OutboxItem]:
        async with self.store.unit_of_work() as uow:
            return await uow.outbox.list_for_load(LOAD_ID)

    async def agent_actions(self) -> list[Event]:
        async with self.store.unit_of_work() as uow:
            events = await uow.events.recent(LOAD_ID, limit=100)
        return [e for e in events if e.type is EventType.AGENT_ACTION]

    async def stored_load(self) -> Load:
        async with self.store.unit_of_work() as uow:
            load = await uow.loads.get(LOAD_ID)
        assert load is not None
        return load


@pytest.fixture
async def world(load: Load) -> _World:
    w = _World(load)
    await w.register()
    return w


async def _a1(world: _World) -> ExecutionResult:
    """07:01: first ETA request and its 30-minute follow-up."""
    return await world.run(
        _risk_high(), ASK_ETA, ScheduleTimer(kind=ETA, due_in_minutes=30), at_time=at(7, 1)
    )


# --- Obligations of any kind: requests and open_obligation (wave 3) ---------------------------


async def test_an_sms_that_requests_a_kind_opens_that_obligation_then_counts_it(
    world: _World,
) -> None:
    ask_pod = SendSms(contact_id=DRIVER, text="Please send the signed POD.", requests="pod")

    await world.run(_driver_sms("m1", at(9, 0), "at the receiver"), ask_pod, at_time=at(9, 1))
    first = await world.obligation()
    await world.run(_driver_sms("m2", at(9, 30), "unloading"), ask_pod, at_time=at(9, 31))

    assert (first.kind, first.attempts, first.last_action_at) == ("pod", 1, at(9, 1))
    again = await world.obligation()
    assert (again.obligation_id, again.attempts) == (first.obligation_id, 2)


async def test_open_obligation_opens_with_no_attempt_and_takes_a_timer(world: _World) -> None:
    result = await world.run(
        _driver_sms("m1", at(9, 0), "at the receiver"),
        OpenObligation(kind="pod"),
        ScheduleTimer(kind="pod", due_in_minutes=120),
        at_time=at(9, 1),
    )

    obligation = await world.obligation()
    assert (obligation.kind, obligation.attempts, obligation.last_action_at) == ("pod", 0, None)
    assert obligation.active_timer_id == "timer_1"
    assert (await world.timers())["timer_1"].due_at == at(11, 1)
    assert result.outbox == ()
    assert result.skipped == ()


async def test_an_email_that_requests_a_kind_opens_and_counts_it_like_an_sms(
    world: _World,
) -> None:
    ask = SendEmail(
        to_contact_ids=(DISPATCHER,), body="Please send the driver's ETA.", requests=ETA
    )

    await world.run(_risk_high(), ask, at_time=at(7, 1))
    first = await world.obligation()
    await world.run(_risk_high("ping_2", at(7, 30)), ASK_ETA, at_time=at(7, 31))

    assert (first.kind, first.attempts, first.last_action_at) == (ETA, 1, at(7, 1))
    again = await world.obligation()
    assert (again.obligation_id, again.attempts, again.last_action_at) == (
        first.obligation_id,
        2,
        at(7, 31),
    )


async def test_an_email_that_requests_nothing_leaves_the_ledger_alone(world: _World) -> None:
    tell = SendEmail(
        to_contact_ids=(DISPATCHER,), body="The driver does not answer.", purpose=Purpose.ESCALATION
    )

    await world.run(_risk_high(), tell, at_time=at(7, 1))

    assert await world.obligations() == []


async def test_open_obligation_is_skipped_when_one_is_already_open(world: _World) -> None:
    await world.run(
        _driver_sms("m1", at(9, 0), "at the receiver"), OpenObligation(kind="pod"), at_time=at(9, 1)
    )

    result = await world.run(
        _driver_sms("m2", at(9, 5), "still here"), OpenObligation(kind="pod"), at_time=at(9, 6)
    )

    assert result.skipped == (
        SkippedAction(
            index=0, type="open_obligation", reason="an open pod obligation already exists"
        ),
    )
    assert len(await world.obligations()) == 1


# --- The main scenario: open, follow up, defer ------------------------------------------------


async def test_a1_first_eta_request_opens_the_obligation_and_schedules_the_follow_up(
    world: _World,
) -> None:
    result = await _a1(world)

    obligation = await world.obligation()
    assert obligation.kind == ETA
    assert obligation.status is Status.OPEN
    assert obligation.attempts == 1
    assert obligation.opened_at == at(7, 1)
    assert obligation.last_action_at == at(7, 1)
    assert obligation.active_timer_id == "timer_1"

    timer = (await world.timers())["timer_1"]
    assert timer.status is TimerStatus.PENDING
    assert timer.obligation_id == obligation.obligation_id
    assert timer.due_at == at(7, 31)
    assert timer.created_at == at(7, 1)

    assert result.obligations == (obligation,)
    assert result.timers == (timer,)
    assert [i.kind for i in result.outbox] == [OutboxKind.SMS]
    assert result.skipped == ()


async def test_a3_second_eta_request_counts_the_attempt_and_the_fired_timer_stays_fired(
    world: _World,
) -> None:
    await _a1(world)
    await world.fire("timer_1", at(7, 31))

    result = await world.run(
        _timer_due("timer_1", at(7, 31)),
        ASK_ETA,
        ScheduleTimer(kind=ETA, due_in_minutes=30),
        at_time=at(7, 31),
    )

    obligation = await world.obligation()
    assert obligation.attempts == 2
    assert obligation.last_action_at == at(7, 31)
    assert obligation.active_timer_id == "timer_2"

    timers = await world.timers()
    assert timers["timer_1"].status is TimerStatus.FIRED
    assert timers["timer_2"].status is TimerStatus.PENDING
    assert timers["timer_2"].due_at == at(8, 1)
    assert result.timers == (timers["timer_2"],)


async def test_g1_deferral_counts_from_the_message_time_and_replaces_the_fired_timer(
    world: _World,
) -> None:
    await _a1(world)
    await world.fire("timer_1", at(7, 31))

    result = await world.run(
        _driver_sms("m_defer", at(7, 30), "give me 5 min"),
        DeferObligation(kind=ETA, minutes=5),
        at_time=at(7, 31),
    )

    obligation = await world.obligation()
    assert obligation.deferred_until == at(7, 35)
    assert obligation.deferrals_used == 1
    assert obligation.active_timer_id == "timer_2"

    timers = await world.timers()
    assert timers["timer_1"].status is TimerStatus.FIRED
    assert timers["timer_2"].status is TimerStatus.PENDING
    assert timers["timer_2"].due_at == at(7, 37)  # deferred_until + 2 minutes of grace
    assert result.obligations == (obligation,)
    assert result.timers == (timers["timer_2"],)
    assert result.outbox == ()


async def test_deferral_supersedes_a_pending_active_timer(world: _World) -> None:
    await _a1(world)

    result = await world.run(
        _driver_sms("m_defer", at(7, 28), "10 min, loading paperwork"),
        DeferObligation(kind=ETA, minutes=10),
        at_time=at(7, 29),
    )

    obligation = await world.obligation()
    assert obligation.deferred_until == at(7, 38)
    assert obligation.active_timer_id == "timer_2"
    timers = await world.timers()
    assert timers["timer_1"].status is TimerStatus.SUPERSEDED
    assert timers["timer_2"].due_at == at(7, 40)
    assert result.timers == (timers["timer_1"], timers["timer_2"])


async def test_deferral_without_an_active_timer_only_defers(world: _World) -> None:
    await world.run(_risk_high(), ASK_ETA, at_time=at(7, 1))

    result = await world.run(
        _driver_sms("m_defer", at(7, 5), "5 min"),
        DeferObligation(kind=ETA, minutes=5),
        at_time=at(7, 6),
    )

    obligation = await world.obligation()
    assert obligation.deferred_until == at(7, 10)
    assert obligation.active_timer_id is None
    assert result.timers == ()
    assert await world.timers() == {}


@pytest.mark.parametrize(
    ("due_in_minutes", "expected_due"),
    [
        pytest.param(5, at(7, 27), id="the promise plus grace wins"),
        pytest.param(30, at(7, 42), id="the requested time wins"),
    ],
)
async def test_schedule_timer_on_a_deferred_obligation_respects_the_promise_plus_grace(
    world: _World, due_in_minutes: int, expected_due: datetime
) -> None:
    await world.run(_risk_high(), ASK_ETA, at_time=at(7, 1))
    await world.run(
        _driver_sms("m_defer", at(7, 10), "15 min"),
        DeferObligation(kind=ETA, minutes=15),
        at_time=at(7, 11),
    )

    await world.run(
        _driver_sms("m_po", at(7, 12), "What's the PO number?"),
        ScheduleTimer(kind=ETA, due_in_minutes=due_in_minutes),
        at_time=at(7, 12),
    )

    obligation = await world.obligation()
    assert obligation.deferred_until == at(7, 25)
    assert obligation.active_timer_id is not None
    timer = (await world.timers())[obligation.active_timer_id]
    assert timer.due_at == expected_due


async def test_schedule_timer_supersedes_every_pending_timer_of_the_obligation(
    world: _World,
) -> None:
    await _a1(world)

    result = await world.run(
        _driver_sms("m_po", at(7, 12), "What's the PO number?"),
        ScheduleTimer(kind=ETA, due_in_minutes=30),
        at_time=at(7, 13),
    )

    timers = await world.timers()
    assert timers["timer_1"].status is TimerStatus.SUPERSEDED
    assert timers["timer_2"].status is TimerStatus.PENDING
    assert timers["timer_2"].due_at == at(7, 43)
    assert (await world.obligation()).active_timer_id == "timer_2"
    assert result.timers == (timers["timer_1"], timers["timer_2"])


async def test_close_obligation_cancels_pending_timers_and_clears_the_active_timer(
    world: _World,
) -> None:
    await _a1(world)
    await world.fire("timer_1", at(7, 31))
    await world.run(
        _timer_due("timer_1", at(7, 31)),
        ASK_ETA,
        ScheduleTimer(kind=ETA, due_in_minutes=30),
        at_time=at(7, 31),
    )

    result = await world.run(
        _driver_sms("m_eta", at(7, 40), "there by 9:15"),
        CloseObligation(kind=ETA, status=CloseStatus.SATISFIED),
        WriteTmsNote(text="Driver ETA to Riverbend plant: 09:15"),
        at_time=at(7, 41),
    )

    obligation = await world.obligation()
    assert obligation.status is Status.SATISFIED
    assert obligation.active_timer_id is None
    timers = await world.timers()
    assert timers["timer_1"].status is TimerStatus.FIRED
    assert timers["timer_2"].status is TimerStatus.CANCELLED
    assert result.obligations == (obligation,)
    assert result.timers == (timers["timer_2"],)


async def test_update_stage_saves_the_load(world: _World) -> None:
    result = await world.run(
        _driver_sms("m_left", at(9, 0), "loaded, leaving now"),
        UpdateStage(stage=Stage.TO_DELIVERY),
        at_time=at(9, 1),
    )

    stored = await world.stored_load()
    assert stored.stage is Stage.TO_DELIVERY
    assert stored == world.load.model_copy(update={"stage": Stage.TO_DELIVERY})
    assert result.load == stored


# --- Document requirements: a stage that requires a document opens its obligation (F5) -------

POD_AT_DELIVERY = DocumentRequirement(
    type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120, opens_obligation="pod"
)


def _requiring(*requirements: DocumentRequirement) -> Broker:
    return standard_broker().model_copy(update={"documents": requirements})


def _pod(status: DocumentStatus, type_id: str = "pod") -> Document:
    return Document(
        document_id="doc_1",
        load_id=LOAD_ID,
        attachment_id="att_1",
        broker_id="northline",
        blob_ref="sha256:x",
        filename="pod.pdf",
        content_type="application/pdf",
        status=status,
        type_id=type_id,
        received_at=at(8, 0),
    )


async def _arrive(world: _World) -> ExecutionResult:
    return await world.run(
        _driver_sms("m_here", at(9, 0), "at the receiver"),
        UpdateStage(stage=Stage.AT_DELIVERY),
        at_time=at(9, 1),
    )


async def test_entering_a_stage_with_a_requirement_opens_its_obligation_and_timer(
    load: Load,
) -> None:
    world = _World(
        load.model_copy(update={"stage": Stage.TO_DELIVERY}), _requiring(POD_AT_DELIVERY)
    )
    await world.register()

    result = await _arrive(world)

    obligation = await world.obligation()
    assert (obligation.kind, obligation.attempts, obligation.last_action_at) == ("pod", 0, None)
    timer = (await world.timers())[obligation.active_timer_id or ""]
    assert timer.due_at == at(11, 1)
    assert timer.reason == "pod due within 120 min"
    assert result.obligations == (obligation,)
    assert result.timers == (timer,)
    assert result.skipped == ()


async def test_a_requirement_without_a_deadline_opens_the_obligation_without_a_timer(
    load: Load,
) -> None:
    requirement = POD_AT_DELIVERY.model_copy(update={"within_minutes": None})
    world = _World(load.model_copy(update={"stage": Stage.TO_DELIVERY}), _requiring(requirement))
    await world.register()

    await _arrive(world)

    obligation = await world.obligation()
    assert obligation.active_timer_id is None
    assert await world.timers() == {}


@pytest.mark.parametrize(
    "requirement",
    [
        POD_AT_DELIVERY.model_copy(update={"opens_obligation": None}),
        POD_AT_DELIVERY.model_copy(update={"at_stage": Stage.DELIVERED}),
    ],
    ids=["opens nothing", "another stage"],
)
async def test_a_requirement_that_opens_nothing_here_changes_no_ledger(
    load: Load, requirement: DocumentRequirement
) -> None:
    world = _World(load.model_copy(update={"stage": Stage.TO_DELIVERY}), _requiring(requirement))
    await world.register()

    await _arrive(world)

    assert await world.obligations() == []


async def test_a_requirement_obligation_already_open_is_not_opened_again(load: Load) -> None:
    world = _World(
        load.model_copy(update={"stage": Stage.TO_DELIVERY}), _requiring(POD_AT_DELIVERY)
    )
    await world.register()
    await world.run(
        _driver_sms("m1", at(8, 0), "almost there"), OpenObligation(kind="pod"), at_time=at(8, 1)
    )

    result = await _arrive(world)

    assert len(await world.obligations()) == 1
    assert result.obligations == ()


async def test_a_clean_document_already_in_hand_satisfies_the_requirement(load: Load) -> None:
    world = _World(
        load.model_copy(update={"stage": Stage.TO_DELIVERY}), _requiring(POD_AT_DELIVERY)
    )
    await world.register()
    await world.save_document(_pod(DocumentStatus.PROCESSED))

    await _arrive(world)

    assert await world.obligations() == []


@pytest.mark.parametrize(
    "document",
    [_pod(DocumentStatus.NEEDS_REVIEW), _pod(DocumentStatus.PROCESSED, type_id="bol")],
    ids=["needs review", "another type"],
)
async def test_a_document_that_is_not_a_clean_one_of_the_type_leaves_the_requirement_open(
    load: Load, document: Document
) -> None:
    world = _World(
        load.model_copy(update={"stage": Stage.TO_DELIVERY}), _requiring(POD_AT_DELIVERY)
    )
    await world.register()
    await world.save_document(document)

    await _arrive(world)

    assert (await world.obligation()).kind == "pod"


async def test_the_deferral_policy_is_the_brokers(load: Load) -> None:
    """A broker that allows 30-minute deferrals: the executor applies its policy."""
    patient = standard_broker().model_copy(
        update={
            "limits": BrokerLimits(
                validation=ValidationConfig(deferral=DeferralPolicy(max_minutes=30))
            )
        }
    )
    world = _World(load, patient)
    await world.register()
    await _a1(world)

    result = await world.run(
        _driver_sms("m_defer", at(7, 10), "give me 25 min"),
        DeferObligation(kind=ETA, minutes=25),
        at_time=at(7, 11),
    )

    assert result.skipped == ()
    assert (await world.obligation()).deferred_until == at(7, 35)


async def test_the_load_is_returned_unchanged_without_a_stage_update(world: _World) -> None:
    result = await world.run(_risk_high(), ASK_ETA, at_time=at(7, 1))

    assert result.load == world.load
    assert await world.stored_load() == world.load


# --- Outbox and the agent's own actions as memory ---------------------------------------------

OUTBOX_CASES = [
    pytest.param(
        SendSms(contact_id=DRIVER, text="PO is 55821.", purpose=Purpose.REPLY),
        OutboxKind.SMS,
        {"contact_id": DRIVER, "text": "PO is 55821.", "purpose": "reply"},
        Channel.SMS,
        "PO is 55821.",
        id="sms",
    ),
    pytest.param(
        SendEmail(
            to_contact_ids=(DISPATCHER,),
            subject="Load 481207: ETA needed",
            body="Your driver does not answer. Please send the ETA.",
            purpose=Purpose.ESCALATION,
        ),
        OutboxKind.EMAIL,
        {
            "to_contact_ids": [DISPATCHER],
            "subject": "Load 481207: ETA needed",
            "body": "Your driver does not answer. Please send the ETA.",
            "purpose": "escalation",
        },
        Channel.EMAIL,
        "Your driver does not answer. Please send the ETA.",
        id="email",
    ),
    pytest.param(
        PostBrokerSlack(text="Load 481207: no ETA from the driver after 30 minutes."),
        OutboxKind.BROKER_SLACK,
        {"broker_id": "northline", "text": "Load 481207: no ETA from the driver after 30 minutes."},
        Channel.SLACK,
        "Load 481207: no ETA from the driver after 30 minutes.",
        id="broker_slack",
    ),
    pytest.param(
        WriteTmsNote(text="ETA requested from the driver."),
        OutboxKind.TMS_NOTE,
        {"text": "ETA requested from the driver."},
        Channel.SYSTEM,
        "ETA requested from the driver.",
        id="tms_note",
    ),
    pytest.param(
        CreateHeroTask(title="Call the driver", description="No ETA after two SMS.", urgent=True),
        OutboxKind.HERO_TASK,
        {"title": "Call the driver", "description": "No ETA after two SMS.", "urgent": True},
        Channel.HERO,
        "Call the driver\nNo ETA after two SMS.",
        id="hero_task",
    ),
    pytest.param(
        CreateHeroTask(title="Review the BOL", description=""),
        OutboxKind.HERO_TASK,
        {"title": "Review the BOL", "description": "", "urgent": False},
        Channel.HERO,
        "Review the BOL",
        id="hero_task_without_description",
    ),
    pytest.param(
        PostInternalSlack(text="Load 481207 is at high delay risk."),
        OutboxKind.INTERNAL_SLACK,
        {"text": "Load 481207 is at high delay risk."},
        Channel.HERO,
        "Load 481207 is at high delay risk.",
        id="internal_slack",
    ),
    pytest.param(
        UpdateStage(stage=Stage.AT_PICKUP),
        OutboxKind.STAGE_UPDATE,
        {"stage": "at_pickup"},
        Channel.SYSTEM,
        "Stage updated to at_pickup",
        id="stage_update",
    ),
]


@pytest.mark.parametrize(("action", "kind", "payload", "channel", "body"), OUTBOX_CASES)
async def test_each_action_kind_puts_its_payload_in_the_outbox(
    world: _World,
    action: Action,
    kind: OutboxKind,
    payload: dict[str, Any],
    channel: Channel,
    body: str,
) -> None:
    result = await world.run(_risk_high(), action, at_time=at(7, 1))

    (item,) = await world.outbox()
    assert item == OutboxItem(
        outbox_id="out_1",
        idempotency_key="risk:ping_1:0",
        run_id="run_1",
        load_id=LOAD_ID,
        kind=kind,
        payload=payload,
        created_at=at(7, 1),
        status=OutboxStatus.PENDING,
    )
    assert result.outbox == (item,)
    assert result.obligations == ()  # only an ETA request touches the ledger
    assert result.timers == ()


@pytest.mark.parametrize(("action", "kind", "payload", "channel", "body"), OUTBOX_CASES)
async def test_each_outbox_item_is_logged_as_an_agent_action_event(
    world: _World,
    action: Action,
    kind: OutboxKind,
    payload: dict[str, Any],
    channel: Channel,
    body: str,
) -> None:
    await world.run(_risk_high(), action, at_time=at(7, 1))

    (logged,) = await world.agent_actions()
    assert logged == Event(
        event_id="act:risk:ping_1:0",
        load_id=LOAD_ID,
        type=EventType.AGENT_ACTION,
        occurred_at=at(7, 1),
        channel=channel,
        direction=Direction.OUTBOUND,
        body=body,
        data={"kind": kind.value, "payload": payload, "run_id": "run_1", "outbox_id": "out_1"},
    )


async def test_eta_request_sms_payload_names_its_purpose(world: _World) -> None:
    result = await _a1(world)

    (item,) = result.outbox
    assert item.payload == {"contact_id": DRIVER, "text": ASK_ETA.text, "purpose": "eta_request"}


async def test_idempotency_key_is_the_event_id_and_the_position_in_the_approved_list(
    world: _World,
) -> None:
    result = await world.run(
        _risk_high(),
        ASK_ETA,
        ScheduleTimer(kind=ETA, due_in_minutes=30),
        WriteTmsNote(text="ETA requested from the driver."),
        PostInternalSlack(text="Load 481207 is at high delay risk."),
        at_time=at(7, 1),
    )

    keys = [i.idempotency_key for i in result.outbox]
    assert keys == ["risk:ping_1:0", "risk:ping_1:2", "risk:ping_1:3"]
    assert [e.event_id for e in await world.agent_actions()] == [f"act:{k}" for k in keys]
    assert [i.outbox_id for i in result.outbox] == ["out_1", "out_2", "out_3"]


# --- Transactions, impossible actions and repeats ---------------------------------------------


async def test_nothing_persists_when_the_unit_of_work_exits_without_commit(
    world: _World,
) -> None:
    result = await world.run(
        _risk_high(),
        ASK_ETA,
        ScheduleTimer(kind=ETA, due_in_minutes=30),
        UpdateStage(stage=Stage.AT_PICKUP),
        at_time=at(7, 1),
        commit=False,
    )

    assert len(result.outbox) == 2  # the executor did its work ...
    assert await world.obligations() == []  # ... but only the caller commits
    assert await world.timers() == {}
    assert await world.outbox() == []
    assert await world.agent_actions() == []
    assert (await world.stored_load()).stage is Stage.TO_PICKUP


async def test_actions_without_an_open_obligation_are_skipped_and_the_rest_still_runs(
    world: _World,
) -> None:
    result = await world.run(
        _risk_high(),
        ScheduleTimer(kind=ETA, due_in_minutes=30),
        WriteTmsNote(text="Delay risk is high."),
        DeferObligation(kind=ETA, minutes=5),
        CloseObligation(kind=ETA, status=CloseStatus.CANCELLED),
        at_time=at(7, 1),
    )

    assert [(s.index, s.type) for s in result.skipped] == [
        (0, "schedule_timer"),
        (2, "defer_obligation"),
        (3, "close_obligation"),
    ]
    assert all("no open eta_request obligation" in s.reason for s in result.skipped)
    assert [i.idempotency_key for i in result.outbox] == ["risk:ping_1:1"]
    assert result.obligations == ()
    assert result.timers == ()
    assert await world.obligations() == []
    assert await world.timers() == {}


async def test_a_deferral_the_policy_forbids_is_skipped_and_changes_nothing(
    world: _World,
) -> None:
    await _a1(world)
    await world.run(
        _driver_sms("m_defer", at(7, 10), "10 min"),
        DeferObligation(kind=ETA, minutes=10),
        at_time=at(7, 11),
    )
    obligation_before = await world.obligation()
    timers_before = await world.timers()

    result = await world.run(
        _driver_sms("m_defer_again", at(7, 20), "5 more min"),
        DeferObligation(kind=ETA, minutes=5),
        at_time=at(7, 21),
    )

    assert result.skipped == (
        SkippedAction(
            index=0, type="defer_obligation", reason="deferral limit reached for this obligation"
        ),
    )
    assert result.obligations == ()
    assert result.timers == ()
    assert await world.obligation() == obligation_before
    assert await world.timers() == timers_before


async def test_an_action_already_in_the_outbox_is_not_applied_twice(world: _World) -> None:
    event = _risk_high()
    await world.run(event, ASK_ETA, at_time=at(7, 1))

    result = await world.run(event, ASK_ETA, at_time=at(7, 2))

    (skipped,) = result.skipped
    assert (skipped.index, skipped.type) == (0, "send_sms")
    assert "risk:ping_1:0" in skipped.reason
    assert result.outbox == ()
    assert result.obligations == ()
    assert (await world.obligation()).attempts == 1
    assert len(await world.outbox()) == 1
    assert len(await world.agent_actions()) == 1


async def test_the_plan_order_is_kept_across_ledger_and_outbox_actions(world: _World) -> None:
    """A close followed by a new ETA request opens a fresh obligation, in plan order."""
    await _a1(world)
    plan: list[Action] = [
        CloseObligation(kind=ETA, status=CloseStatus.CANCELLED),
        ASK_ETA,
        ScheduleTimer(kind=ETA, due_in_minutes=30),
    ]

    result = await world.run(_risk_high("ping_9", at(9, 0)), *plan, at_time=at(9, 1))

    first, second = await world.obligations()
    assert first.status is Status.CANCELLED
    assert second.status is Status.OPEN
    assert second.attempts == 1
    assert second.active_timer_id == "timer_2"
    assert result.obligations == (first, second)
    assert (await world.timers())["timer_1"].status is TimerStatus.CANCELLED
