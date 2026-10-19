"""The runtime facade end to end: ingest, timers, runs through the graph, delivery.

Every test drives `AgentRuntime` the way the eval runner and the API do, on the
memory store, a virtual clock, a scripted model and recording gateways.
"""

import pytest

from freight_agent.application.contracts import IngestResult
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.actions import (
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    DeferObligation,
    Purpose,
    SendEmail,
    SendSms,
)
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, DelayRisk, Load
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.outbox import OutboxKind, OutboxStatus
from freight_agent.domain.ping_filter import PingFilterConfig, RiskTracker
from freight_agent.domain.runs import RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from tests.unit.application.test_runtime_harness import (
    ASK_AGAIN,
    ASK_ETA,
    DECIDE,
    DISPATCHER,
    DRIVER,
    ETA,
    FOLLOW_UP_IN_30,
    GATE,
    LOAD_ID,
    World,
    at,
    decision,
    plan,
)

pytestmark = pytest.mark.unit


# --- the assignment's sequence (Client A) ---------------------------------------------------


async def test_assignment_sequence_for_client_a(load: Load) -> None:
    world = await World(load).start()

    # 07:00 the ping turns the risk high; 07:01 the agent asks the driver for the ETA.
    ingest = await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    assert ingest == IngestResult(accepted=True, event_id="risk:p1")
    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
    runs = await world.tick(at(7, 1))

    assert [(r.event_id, r.status) for r in runs] == [("risk:p1", RunStatus.ACTED)]
    run = runs[0]
    assert run.gate is not None and run.gate.intent is Intent.DELAY_RISK_HIGH
    assert "delay_risk_high" in run.sop_section_ids
    assert (run.started_at, run.finished_at) == (at(7, 1), at(7, 1))
    assert [d.payload["contact_id"] for d in world.sent("sms")] == [DRIVER]
    snapshot = await world.snapshot()
    assert snapshot.load.delay_risk is DelayRisk.HIGH
    obligation = await world.eta_request()
    assert obligation is not None and obligation.is_open and obligation.attempts == 1
    assert [t.due_at for t in await world.pending_timers()] == [at(7, 31)]
    assert {i.status for i in snapshot.outbox} == {OutboxStatus.SENT}

    # 07:12 the driver asks for the PO; 07:13 the agent answers from the instructions.
    await world.message(at(7, 12), "What's the PO number?")
    world.model.push(GATE, decision(Intent.QUESTION))
    reply = SendSms(contact_id=DRIVER, text="The PO number is 55821.", purpose=Purpose.REPLY)
    world.model.push(DECIDE, plan(reply))
    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.ACTED]
    assert runs[0].sop_section_ids == ("communication_rules", "driver_questions", "escalation")
    decide_prompt = world.model.calls[-1].request.user
    assert "delay_risk_high" in decide_prompt  # the ETA procedure in progress, as context
    assert "PO 55821" in decide_prompt
    assert world.sent("sms")[-1].payload["text"] == "The PO number is 55821."
    obligation = await world.eta_request()
    assert obligation is not None and obligation.is_open and obligation.attempts == 1
    assert [t.due_at for t in await world.pending_timers()] == [at(7, 31)]

    # 07:31 the timer: no ETA yet, so the second SMS and a new timer.
    world.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))
    runs = await world.tick(at(7, 31))

    assert [(r.event_id, r.status) for r in runs] == [("timer:timer_1", RunStatus.ACTED)]
    assert runs[0].gate is not None and runs[0].gate.intent is Intent.ETA_FOLLOW_UP_DUE
    assert len(world.sent("sms")) == 3
    obligation = await world.eta_request()
    assert obligation is not None and obligation.attempts == 2
    assert [t.due_at for t in await world.pending_timers()] == [at(8, 1)]
    timers = {t.timer_id: t.status for t in (await world.snapshot()).timers}
    assert timers == {"timer_1": TimerStatus.FIRED, "timer_2": TimerStatus.PENDING}

    # 08:01 the second timer: email the dispatcher, a Hero task, the request is exhausted.
    email = SendEmail(
        to_contact_ids=(DISPATCHER,),
        subject="Load 481207: ETA needed",
        body="Your driver does not answer. Please send the ETA.",
        purpose=Purpose.ESCALATION,
    )
    task = CreateHeroTask(
        title="Call the driver for the ETA",
        description="Two SMS and an email to the dispatcher got no ETA.",
        urgent=True,
    )
    world.model.push(
        DECIDE, plan(email, task, CloseObligation(kind=ETA, status=CloseStatus.EXHAUSTED))
    )
    runs = await world.tick(at(8, 1))

    assert [(r.event_id, r.status) for r in runs] == [("timer:timer_2", RunStatus.ACTED)]
    assert [d.payload["to_contact_ids"] for d in world.sent("email")] == [[DISPATCHER]]
    assert [d.payload["title"] for d in world.sent("hero_task")] == ["Call the driver for the ETA"]
    assert len(world.sent("sms")) == 3  # no third SMS
    obligation = await world.eta_request()
    assert obligation is not None and obligation.status is ObligationStatus.EXHAUSTED
    assert await world.pending_timers() == []
    assert await world.runtime.next_timer_due_at() is None

    # One gate call (the question) and four plans; nothing scripted is left over.
    assert (world.calls(GATE), world.calls(DECIDE), world.model.remaining()) == (1, 4, 0)
    snapshot = await world.snapshot()
    assert [r.status for r in snapshot.runs] == [RunStatus.ACTED] * 4
    assert {i.status for i in snapshot.outbox} == {OutboxStatus.SENT}
    actions = [e for e in snapshot.events if e.type is EventType.AGENT_ACTION]
    assert len(actions) == len(snapshot.outbox) == 5  # three SMS, the email, the task


async def test_acknowledgement_after_the_eta_request_is_a_noop(load: Load) -> None:
    """B2: "ok" is not an ETA. One gate call, no plan, the request stays open."""
    world = await World(load).start()
    await world.high_risk_at_seven()

    await world.message(at(7, 5), "ok")
    world.model.push(GATE, decision(Intent.ACKNOWLEDGEMENT, act=False))
    runs = await world.tick(at(7, 6))

    assert [r.status for r in runs] == [RunStatus.NOOP]
    assert runs[0].gate is not None and runs[0].gate.intent is Intent.ACKNOWLEDGEMENT
    assert len(runs[0].usage) == 1 and runs[0].usage[0].role == "gate"
    assert runs[0].plan is None
    assert (world.calls(GATE), world.calls(DECIDE)) == (1, 1)
    assert len(world.sent("sms")) == 1
    obligation = await world.eta_request()
    assert obligation is not None and obligation.is_open and obligation.attempts == 1
    assert [t.due_at for t in await world.pending_timers()] == [at(7, 31)]


async def test_deferral_makes_the_old_timer_stale(load: Load) -> None:
    """G1: a deferral at 07:30 races the 07:31 timer; the old timer does nothing."""
    world = await World(load).start()
    await world.high_risk_at_seven()

    await world.message(at(7, 30), "I'll tell you in 5 min")
    world.model.push(GATE, decision(Intent.ETA_DEFERRED))
    world.model.push(DECIDE, plan(DeferObligation(kind=ETA, minutes=5)))
    runs = await world.tick(at(7, 31))  # the 07:31 timer fires in the same tick

    assert [(r.event_id, r.status, r.skip_reason) for r in runs] == [
        ("msg:0730", RunStatus.ACTED, None),
        ("timer:timer_1", RunStatus.SKIPPED, "stale_timer"),
    ]
    assert len(world.sent("sms")) == 1
    obligation = await world.eta_request()
    assert obligation is not None and obligation.deferred_until == at(7, 35)
    assert [t.due_at for t in await world.pending_timers()] == [at(7, 37)]

    assert await world.tick(at(7, 36)) == []
    world.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))
    runs = await world.tick(at(7, 37))

    assert [(r.event_id, r.status) for r in runs] == [("timer:timer_2", RunStatus.ACTED)]
    assert len(world.sent("sms")) == 2
    obligation = await world.eta_request()
    assert obligation is not None and obligation.attempts == 2


# --- ingest ---------------------------------------------------------------------------------


async def test_a_duplicate_message_id_is_accepted_once(load: Load) -> None:
    world = await World(load).start()

    first = await world.message(at(7, 12), "Running late", message_id="m-1")
    again = await world.message(at(7, 12), "Running late", message_id="m-1")

    assert first == IngestResult(accepted=True, event_id="m-1")
    assert again == IngestResult(accepted=True, duplicate=True, event_id="m-1")
    world.model.push(GATE, decision(Intent.STATUS_UPDATE, act=False))
    runs = await world.tick(at(7, 13))
    assert [r.event_id for r in runs] == ["m-1"]
    assert world.calls(GATE) == 1


async def test_a_message_keeps_its_fields_and_attachments(load: Load) -> None:
    world = await World(load).start()
    photo = Attachment(
        attachment_id="a1", filename="bol.jpg", content_type="image/jpeg", ref="s3://bol"
    )

    await world.message(at(7, 12), "", channel=Channel.SMS, attachments=(photo,))

    event = next(e for e in (await world.snapshot()).events if e.event_id == "msg:0712")
    assert event.type is EventType.INBOUND_MESSAGE
    assert (event.sender_contact_id, event.channel, event.occurred_at) == (
        DRIVER,
        Channel.SMS,
        at(7, 12),
    )
    assert event.attachments == (photo,)


async def test_messages_and_pings_for_an_unknown_load_are_refused(load: Load) -> None:
    world = World(load)  # never registered

    message = await world.message(at(7, 12), "hello")
    ping = await world.ping(at(7, 13), DelayRisk.HIGH)

    assert message.accepted is False and "481207" in (message.reason or "")
    assert ping.accepted is False and "481207" in (ping.reason or "")
    assert await world.runtime.tick() == []


async def test_snapshot_reads_every_event_of_a_long_history(load: Load) -> None:
    world = await World(load).start()
    async with world.store.unit_of_work() as uow:
        for index in range(300):
            await uow.events.append(
                Event(
                    event_id=f"m-{index}",
                    load_id=LOAD_ID,
                    type=EventType.INBOUND_MESSAGE,
                    occurred_at=at(7, 0),
                    channel=Channel.SMS,
                    sender_contact_id=DRIVER,
                    body="ok",
                )
            )
        await uow.commit()

    events = (await world.snapshot()).events

    assert [e.event_id for e in events] == [f"m-{index}" for index in range(300)]


async def test_snapshot_of_an_unknown_load_raises(load: Load) -> None:
    with pytest.raises(DomainError):
        await World(load).runtime.snapshot("nope")


async def test_an_empty_message_is_skipped_without_a_model_call(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "   ")

    runs = await world.tick(at(7, 13))

    assert [(r.status, r.skip_reason) for r in runs] == [(RunStatus.SKIPPED, "empty_message")]
    assert world.model.calls == []


async def test_an_event_queued_twice_is_skipped_the_second_time(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "What's the PO?", message_id="m-1")
    async with world.store.unit_of_work() as uow:  # a redelivery that bypassed ingest
        event = await uow.events.get("m-1")
        assert event is not None
        await uow.queue.enqueue(event)
        await uow.commit()
    world.model.push(GATE, decision(Intent.QUESTION))
    world.model.push(DECIDE, plan())

    runs = await world.tick(at(7, 13))

    assert [(r.status, r.skip_reason) for r in runs] == [
        (RunStatus.NOOP, None),
        (RunStatus.SKIPPED, "already_processed"),
    ]


# --- pings ----------------------------------------------------------------------------------


async def test_register_load_starts_the_tracker_at_the_load_risk(load: Load) -> None:
    world = await World(load.model_copy(update={"delay_risk": DelayRisk.MEDIUM})).start()

    async with world.store.unit_of_work() as uow:
        tracker = await uow.loads.get_tracker(LOAD_ID)

    assert tracker == RiskTracker(band=DelayRisk.MEDIUM)


async def test_a_high_ping_becomes_a_risk_event(load: Load) -> None:
    world = await World(load).start()

    result = await world.ping(at(7, 0), DelayRisk.HIGH, "p7")

    assert result == IngestResult(accepted=True, event_id="risk:p7")
    snapshot = await world.snapshot()
    event = next(e for e in snapshot.events if e.event_id == "risk:p7")
    assert event.type is EventType.RISK_CHANGED
    assert (event.channel, event.occurred_at) == (Channel.SYSTEM, at(7, 0))
    assert event.data == {"previous": "low", "current": "high"}
    assert snapshot.load.delay_risk is DelayRisk.HIGH


async def test_medium_is_tracked_but_does_not_wake_the_agent(load: Load) -> None:
    world = await World(load).start()

    result = await world.ping(at(7, 0), DelayRisk.MEDIUM, "p1")

    assert result == IngestResult(accepted=True)
    snapshot = await world.snapshot()
    assert snapshot.load.delay_risk is DelayRisk.MEDIUM
    assert snapshot.events == ()
    assert await world.runtime.tick() == []


async def test_a_downgrade_needs_two_pings_and_a_duplicate_does_not_count(load: Load) -> None:
    world = await World(load).start()
    await world.high_risk_at_seven()

    first = await world.ping(at(7, 10), DelayRisk.LOW, "p2")
    duplicate = await world.ping(at(7, 11), DelayRisk.LOW, "p2")

    assert first == IngestResult(accepted=True)
    assert duplicate.accepted and duplicate.duplicate
    assert (await world.snapshot()).load.delay_risk is DelayRisk.HIGH
    assert await world.tick(at(7, 12)) == []

    confirmed = await world.ping(at(7, 15), DelayRisk.LOW, "p3")

    assert confirmed == IngestResult(accepted=True, event_id="risk:p3")
    assert (await world.snapshot()).load.delay_risk is DelayRisk.LOW
    world.model.push(DECIDE, plan(CloseObligation(kind=ETA, status=CloseStatus.CANCELLED)))
    runs = await world.tick(at(7, 16))

    assert [r.status for r in runs] == [RunStatus.ACTED]
    assert runs[0].gate is not None and runs[0].gate.intent is Intent.DELAY_RISK_LOW
    assert runs[0].sop_section_ids == ("communication_rules", "delay_risk_cleared", "escalation")
    obligation = await world.eta_request()
    assert obligation is not None and obligation.status is ObligationStatus.CANCELLED
    assert await world.pending_timers() == []


async def test_the_ping_memory_is_bounded(load: Load) -> None:
    config = RuntimeConfig(
        ping_filter=PingFilterConfig(downgrade_confirmations=3), recent_pings_per_load=1
    )
    world = await World(
        load.model_copy(update={"delay_risk": DelayRisk.HIGH}), config=config
    ).start()

    await world.ping(at(7, 0), DelayRisk.LOW, "p1")
    await world.ping(at(7, 1), DelayRisk.LOW, "p2")
    again = await world.ping(at(7, 2), DelayRisk.LOW, "p1")  # p1 was forgotten (window of 1)

    assert again == IngestResult(accepted=True, event_id="risk:p1")


async def test_a_risk_event_already_logged_is_not_queued_again(load: Load) -> None:
    world = await World(load).start()
    async with world.store.unit_of_work() as uow:  # logged by an earlier process
        await uow.events.append(
            Event(
                event_id="risk:p1",
                load_id=LOAD_ID,
                type=EventType.RISK_CHANGED,
                occurred_at=at(7, 0),
                channel=Channel.SYSTEM,
                data={"previous": "low", "current": "high"},
            )
        )
        await uow.commit()

    result = await world.ping(at(7, 0), DelayRisk.HIGH, "p1")

    assert result == IngestResult(accepted=True, duplicate=True, event_id="risk:p1")
    assert await world.tick(at(7, 1)) == []


# --- timers ---------------------------------------------------------------------------------


async def test_fire_due_timers_turns_due_timers_into_events(load: Load) -> None:
    world = await World(load).start()
    await world.high_risk_at_seven()
    assert await world.runtime.next_timer_due_at() == at(7, 31)

    world.clock.set(at(7, 30))
    assert await world.runtime.fire_due_timers() == 0
    world.clock.set(at(7, 40))
    assert await world.runtime.fire_due_timers() == 1
    assert await world.runtime.fire_due_timers() == 0

    snapshot = await world.snapshot()
    assert [t.status for t in snapshot.timers] == [TimerStatus.FIRED]
    event = next(e for e in snapshot.events if e.type is EventType.TIMER_DUE)
    assert event.event_id == "timer:timer_1"
    # Stamped when it fired (07:40); the due time stays in the data, for timer lag.
    assert (event.channel, event.occurred_at) == (Channel.SYSTEM, at(7, 40))
    assert event.data == {
        "timer_id": "timer_1",
        "obligation_id": "obl_1",
        "due_at": at(7, 31).isoformat(),
        "kind": "eta_request",
    }


async def test_a_timer_event_already_logged_is_not_queued_again(load: Load) -> None:
    world = await World(load).start()
    await world.high_risk_at_seven()
    async with world.store.unit_of_work() as uow:
        await uow.events.append(
            Event(
                event_id="timer:timer_1",
                load_id=LOAD_ID,
                type=EventType.TIMER_DUE,
                occurred_at=at(7, 31),
                channel=Channel.SYSTEM,
            )
        )
        await uow.commit()

    world.clock.set(at(7, 31))
    assert await world.runtime.fire_due_timers() == 1
    async with world.store.unit_of_work() as uow:
        assert await uow.queue.size() == 0


async def test_a_timer_of_a_missing_obligation_fires_and_is_skipped(load: Load) -> None:
    world = await World(load).start()
    async with world.store.unit_of_work() as uow:
        await uow.timers.save(
            Timer(
                timer_id="timer_x",
                load_id=LOAD_ID,
                obligation_id="obl_gone",
                due_at=at(7, 0),
                created_at=at(6, 0),
            )
        )
        await uow.commit()

    runs = await world.tick(at(7, 0))

    assert [(r.status, r.skip_reason) for r in runs] == [(RunStatus.SKIPPED, "stale_timer")]
    event = next(e for e in (await world.snapshot()).events if e.event_id == "timer:timer_x")
    assert event.data["kind"] is None


async def test_tick_returns_every_run_and_delivers_the_outbox(load: Load) -> None:
    world = await World(load).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    await world.message(at(7, 0, 30), "ok")
    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
    world.model.push(GATE, decision(Intent.ACKNOWLEDGEMENT, act=False))

    runs = await world.tick(at(7, 1))

    assert [r.status for r in runs] == [RunStatus.ACTED, RunStatus.NOOP]
    assert [o.kind for o in (await world.snapshot()).outbox] == [OutboxKind.SMS]
    assert len(world.sent("sms")) == 1
    assert await world.runtime.process_next() is None


@pytest.mark.unit
async def test_every_run_that_reads_the_sop_records_its_version(load: Load) -> None:
    """An action can always be traced to the exact SOP text it followed."""
    world = await World(load).start()

    (run,) = await world.high_risk_at_seven()

    assert run.sop_version == "test"
    assert "delay_risk_high" in run.sop_section_ids
